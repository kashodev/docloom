"""Distributed catalogue build — a build is a run.

The single-process build holds the whole catalogue in RAM and uploads once at the
end: fine for 300k, but memory-bound, single-task, and all-or-nothing. This
drives the build the way document generation is driven — as a *run* over work
units — so it reuses the proven coordination and gains bounded memory, horizontal
scale, incremental writes, resume, and a fleet-wide budget.

The mapping is exact (see ``feature_explorations/catalogue-build-scaling.md``):

* a **unit** is a contiguous range of company indices, from ``plan_units``;
* workers **claim** units atomically from the StateStore — N tasks, no collision;
* each unit builds only its range (companies are per-index seeded, so a range is
  independent), writes a **shard pair** and a manifest **part**, then completes;
* the **root manifest** is assembled from the parts at completion — its presence
  is the done signal, and it refuses a gap;
* a **DistributedBudgetGuard** caps spend across the whole fleet, which for a paid
  build is the point, not a nicety.

The artifact lives under one ``out`` URI, exactly like the single-file build's
``--out``: ``out/shards/…``, ``out/catalogue-parts/…``, ``out/manifest.json``. The
StateStore (a separate URI) only coordinates who builds which unit.

Procedural and LLM builds share this; the only difference is how a unit's products
are produced. A build with no provider mix is key-free and free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.logging import bind, bound, get_logger
from docloom.core.pipeline.run import create_run
from docloom.core.providers.budget import BudgetGuard, DistributedBudgetGuard
from docloom.core.providers.mix import ProviderMix
from docloom.core.state.base import StateStore, WorkUnit
from docloom.core.storage import open_store
from docloom.core.storage.base import BlobStore
from docloom.packs.invoice.artifact import (
    MANIFEST_KEY,
    write_catalogue_shard,
    write_sharded_manifest,
)
from docloom.packs.invoice.llm_build import build_llm_catalogue_sync
from docloom.packs.invoice.procedural import generate_company_range

_log = get_logger(__name__)

#: The build's units are namespaced so a catalogue build and a document run never
#: share a run id in the StateStore.
CATALOGUE_PACK = "invoice-catalogue"

_PARTS_DIR = "catalogue-parts"


def _part_key(unit_index: int) -> str:
    return f"{_PARTS_DIR}/unit-{unit_index:06d}.json"


@dataclass(slots=True)
class BuildStats:
    """What one worker accomplished over a build."""

    units_completed: int = 0
    units_failed: int = 0
    companies: int = 0
    products: int = 0
    llm_filled: int = 0
    procedural_fallback: int = 0
    total_cost: Decimal = Decimal(0)


def build_catalogue_run(
    state: StateStore,
    *,
    out: str,
    build_id: str,
    catalogue_version: str,
    companies: int,
    products_per_company: int = 300,
    unit_size: int = 200,
    seed: int = 0,
    mix: ProviderMix | None = None,
    budget_usd: float | None = None,
    concurrency: int = 8,
    max_rounds: int = 3,
    provenance: dict | None = None,
) -> BuildStats:
    """Plan (idempotently) and work a catalogue build as one worker.

    Call it from every task at once, exactly like ``generate``: the StateStore's
    conditional create plans once, the atomic claim keeps workers off each
    other's units, and whoever finishes the last unit writes the root manifest.
    """
    bind(build_id=build_id)
    artifact = open_store(out)
    create_run(state, run_id=build_id, pack=CATALOGUE_PACK, config_id=catalogue_version,
               total=companies, unit_size=unit_size)

    # A fleet-wide budget: N workers sharing one cap against the run's spend
    # rollup, so a paid build cannot spend budget × workers.
    budget: BudgetGuard | DistributedBudgetGuard | None = None
    if mix is not None and budget_usd:
        budget = DistributedBudgetGuard(state, build_id, Decimal(str(budget_usd)))

    stats = BuildStats()
    _log.info("catalogue build: worker draining", companies=companies, unit_size=unit_size,
              out=out, mode="llm" if mix is not None else "procedural")
    while (unit := state.claim_next_unit(build_id)) is not None:
        _work_unit(state, artifact, out, build_id, unit, stats,
                   products_per_company=products_per_company, seed=seed, mix=mix,
                   budget=budget, concurrency=concurrency, max_rounds=max_rounds)

    _finalize_if_complete(state, artifact, out, build_id, catalogue_version, provenance)
    _log.info("catalogue build: worker drained", units=stats.units_completed,
              failed=stats.units_failed, products=stats.products, cost=str(stats.total_cost))
    return stats


def _work_unit(
    state: StateStore, artifact: BlobStore, out: str, build_id: str, unit: WorkUnit,
    stats: BuildStats, *, products_per_company: int, seed: int,
    mix: ProviderMix | None, budget: BudgetGuard | DistributedBudgetGuard | None,
    concurrency: int, max_rounds: int,
) -> None:
    with bound(unit=unit.unit_index, companies=f"{unit.start_index}..{unit.end_index}"):
        try:
            if mix is None:
                rows, products = generate_company_range(
                    unit.start_index, unit.end_index,
                    products_per_company=products_per_company, seed=seed,
                )
                report = None
            else:
                rows, products, report = build_llm_catalogue_sync(
                    mix, companies=unit.count, company_start=unit.start_index,
                    products_per_company=products_per_company, seed=seed,
                    budget=budget, concurrency=concurrency, max_rounds=max_rounds,
                )
            descriptor = write_catalogue_shard(out, unit.unit_index,
                                               companies=rows, products=products)
            # The descriptor part lands before the unit is marked done, so the
            # root — assembled at completion — never reads a done unit with no
            # part. Same ordering guarantee as the run manifest.
            artifact.put(_part_key(unit.unit_index),
                         json.dumps(descriptor).encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            state.fail_unit(build_id, unit.unit_index, repr(exc))
            stats.units_failed += 1
            _log.warning("catalogue unit failed", error=repr(exc), exc_info=exc)
            return

        state.complete_unit(build_id, unit.unit_index)
        stats.units_completed += 1
        stats.companies += len(rows)
        stats.products += descriptor["products"]
        if report is not None:
            stats.llm_filled += report.llm_filled
            stats.procedural_fallback += report.procedural_fallback
            stats.total_cost += report.total_cost
        _log.info("catalogue unit completed", companies=len(rows),
                  products=descriptor["products"],
                  llm_filled=report.llm_filled if report else None)


def _finalize_if_complete(
    state: StateStore, artifact: BlobStore, out: str, build_id: str,
    catalogue_version: str, provenance: dict | None,
) -> None:
    """Write the root manifest once every unit is done, from the shard parts."""
    progress = state.progress(build_id)
    outstanding = progress[WorkUnitState.PENDING] + progress[WorkUnitState.RUNNING]
    if outstanding or progress[WorkUnitState.FAILED]:
        if progress[WorkUnitState.FAILED]:
            _log.warning("catalogue build left incomplete",
                         failed=progress[WorkUnitState.FAILED], pending=outstanding)
        return

    run = state.get_run(build_id)
    if run is None or run.state is RunState.COMPLETED:
        return   # someone already finalised
    state.set_run_state(build_id, RunState.COMPLETED)

    shards = [json.loads(artifact.get(key))
              for key in sorted(artifact.iter_keys(f"{_PARTS_DIR}/"))]
    total_provenance = {
        **(provenance or {}),
        "units": len(shards),
        "companies": sum(s["companies"] for s in shards),
        "products": sum(s["products"] for s in shards),
    }
    write_sharded_manifest(out, catalogue_version=catalogue_version, shards=shards,
                           provenance=total_provenance)
    _log.info("catalogue build complete", units=len(shards),
              companies=total_provenance["companies"],
              products=total_provenance["products"], manifest=f"{out}/{MANIFEST_KEY}")
