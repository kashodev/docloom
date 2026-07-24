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
from docloom.packs.invoice.llm_build import BuildReport, build_llm_catalogue_sync
from docloom.packs.invoice.procedural import generate_company_range

_log = get_logger(__name__)

#: The build's units are namespaced so a catalogue build and a document run never
#: share a run id in the StateStore.
CATALOGUE_PACK = "invoice-catalogue"

_PARTS_DIR = "catalogue-parts"


def _part_key(unit_index: int) -> str:
    return f"{_PARTS_DIR}/unit-{unit_index:06d}.json"


def _shard_fallback(report: "BuildReport | None") -> dict | None:
    """Per-shard fallback record for the manifest (R3): which providers this
    shard found dead, and how its fill split. ``None`` for a procedural build,
    or an LLM build where nothing was quarantined — so a clean shard adds no
    noise, and a corpus stays auditable for which shards degraded and why."""
    if report is None or not report.quarantined:
        return None
    return {
        "quarantined": sorted(report.quarantined),
        "llm_filled": report.llm_filled,
        "procedural_fallback": report.procedural_fallback,
    }


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
    #: Whether the *build* (not this worker) finished — i.e. every unit is done
    #: and the root manifest exists. A worker that legitimately claims nothing
    #: still has to report this, or an incomplete build exits 0 and reads as a
    #: success. See ``build_catalogue_run``.
    build_complete: bool = False
    #: Whether the build has FAILED units (holes) at the end of this worker's
    #: drain. This — not ``build_complete`` — is what a failure exit code keys on.
    #: A worker that finished its share cleanly while peers are still mid-unit is
    #: not a failure (the build simply isn't globally done yet); only real holes
    #: are. Conflating the two makes every early-finisher exit non-zero and get
    #: pointlessly retried.
    build_has_failures: bool = False


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
    # Return previously failed units to the pool before draining. Without this a
    # unit that fails once is failed forever: the claim only hands out PENDING
    # units, so a retried task finds nothing to do, drains cleanly and exits 0 —
    # reporting success for a build that is still full of holes. Bounded by
    # construction: the reset happens once per invocation, so a unit can fail at
    # most once per attempt rather than spinning inside one drain.
    requeued = state.reset_failed_units(build_id)
    if requeued:
        _log.info("catalogue build: re-queued failed units", requeued=requeued)

    _log.info("catalogue build: worker started", companies=companies, unit_size=unit_size,
              out=out, mode="llm" if mix is not None else "procedural")
    while (unit := state.claim_next_unit(build_id)) is not None:
        _work_unit(state, artifact, out, build_id, unit, stats,
                   products_per_company=products_per_company, seed=seed, mix=mix,
                   budget=budget, concurrency=concurrency, max_rounds=max_rounds)

    stats.build_complete = _finalize_if_complete(
        state, artifact, out, build_id, catalogue_version, provenance)
    stats.build_has_failures = state.progress(build_id)[WorkUnitState.FAILED] > 0
    _log.info("catalogue build: worker finished", units=stats.units_completed,
              failed=stats.units_failed, products=stats.products,
              cost=str(stats.total_cost), build_complete=stats.build_complete,
              build_has_failures=stats.build_has_failures)
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
                # TODO(progress): emit intra-unit progress logs (~every 5% of the
                # unit's products filled) between "worker started" and "unit
                # completed". `_run_concurrent` now consumes results as they land
                # (the as_completed rework), so the hard part is done — what's left
                # is to invoke a progress callback in that loop, thread it through
                # build_llm_catalogue's round loop, throttle to 5% boundaries of
                # report.products, and log it via `_log.info` under this bound
                # `unit` context. Small now.
                # Seed the breaker from what earlier units already learned, so a
                # dead provider is not re-discovered (and re-paid) unit by unit;
                # flush anything this unit newly quarantines back for later ones.
                known_bad = state.quarantined_providers(build_id)
                rows, products, report = build_llm_catalogue_sync(
                    mix, companies=unit.count, company_start=unit.start_index,
                    products_per_company=products_per_company, seed=seed,
                    budget=budget, concurrency=concurrency, max_rounds=max_rounds,
                    quarantined=known_bad,
                )
                newly_bad = report.quarantined - known_bad
                if newly_bad:
                    state.quarantine_providers(build_id, report.quarantined)
                    _log.warning("providers quarantined for the build",
                                 newly=sorted(newly_bad), all=sorted(report.quarantined))
            descriptor = write_catalogue_shard(out, unit.unit_index,
                                               companies=rows, products=products,
                                               fallback=_shard_fallback(report))
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
) -> bool:
    """Write the root manifest once every unit is done, from the shard parts.

    Returns whether the *build* is complete — including when another worker
    finalised it — so the caller can set an exit code that reflects the build
    rather than this worker's share of it.
    """
    progress = state.progress(build_id)
    outstanding = progress[WorkUnitState.PENDING] + progress[WorkUnitState.RUNNING]
    if outstanding or progress[WorkUnitState.FAILED]:
        _log.warning("catalogue build left incomplete",
                     failed=progress[WorkUnitState.FAILED], pending=outstanding)
        return False

    run = state.get_run(build_id)
    if run is None:
        return False
    if run.state is RunState.COMPLETED:
        return True   # someone already finalised
    state.set_run_state(build_id, RunState.COMPLETED)

    shards = [json.loads(artifact.get(key))
              for key in sorted(artifact.iter_keys(f"{_PARTS_DIR}/"))]
    total_provenance = {
        **(provenance or {}),
        "units": len(shards),
        "companies": sum(s["companies"] for s in shards),
        "products": sum(s["products"] for s in shards),
    }
    # R3: a build-level summary of where a provider outage bit — which shards
    # degraded and which providers were quarantined — so the corpus is auditable
    # from the root alone. Each shard's descriptor keeps its own detail.
    degraded = [s["unit_index"] for s in shards if s.get("fallback")]
    if degraded:
        total_provenance["fallback"] = {
            "shards_degraded": degraded,
            "quarantined_providers": sorted(
                {p for s in shards if s.get("fallback")
                 for p in s["fallback"]["quarantined"]}),
        }
    write_sharded_manifest(out, catalogue_version=catalogue_version, shards=shards,
                           provenance=total_provenance)
    _log.info("catalogue build complete", units=len(shards),
              companies=total_provenance["companies"],
              products=total_provenance["products"], manifest=f"{out}/{MANIFEST_KEY}")
    return True
