"""Run orchestration — plan, create, drive, resume.

Thin glue over the planner, the state store, and the worker. Its job is the run
*lifecycle*: divide the work, record it, run it to a terminal state, and let a
later invocation resume what did not finish.

The functions here are what a CLI (or a Cloud Run task) calls. They are
deliberately not a class: a run's identity lives in the StateStore, not in a
Python object, so any process that can reach the store can start, resume, pause,
or cancel a run.
"""

from __future__ import annotations

import os
import time

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.logging import bind, get_logger
from docloom.core.pipeline.planner import plan_units
from docloom.core.pipeline.renderer import DocumentRenderer
from docloom.core.pipeline.manifest import write_run_manifest
from docloom.core.pipeline.source import DocumentSource, prepare_source
from docloom.core.pipeline.worker import GenerationWorker, WorkerStats
from docloom.core.state.base import Run, StateStore
from docloom.core.storage.base import BlobStore

_log = get_logger(__name__)


def create_run(
    state: StateStore,
    *,
    run_id: str,
    pack: str,
    config_id: str,
    total: int,
    unit_size: int,
    wait_timeout: float = 180.0,
) -> Run:
    """Plan a run into units and record it, ready to be worked.

    Safe to call from every worker simultaneously, which is exactly what a Cloud
    Run job or an AWS Batch array does: the store makes creation a conditional
    write, so one worker plans and the rest wait for that plan to land.
    """
    units = plan_units(run_id, total, unit_size)
    run = Run(run_id=run_id, pack=pack, config_id=config_id, total_units=len(units),
              state=RunState.RUNNING)
    if state.create_run(run, units):
        _log.info("run planned", pack=pack, config_id=config_id,
                  total=total, units=len(units), unit_size=unit_size)
    else:
        # Another worker won the race to plan this run. It may still be writing
        # units, and claiming from a half-written plan would look like an empty
        # run, so wait for it to finish rather than racing ahead.
        _log.info("another worker is planning this run; waiting")
        _await_plan(state, run_id, timeout=wait_timeout)
    return run


def _await_plan(state: StateStore, run_id: str, *, timeout: float, interval: float = 0.5) -> None:
    """Block until another worker's plan is complete.

    Raises rather than returning quietly on timeout: a worker that proceeds
    against an unplanned run claims nothing, exits successfully, and reports a
    finished run that generated no documents — the failure hardest to notice.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = state.get_run(run_id)
        if run is not None and run.planned:
            return
        time.sleep(interval)
    raise TimeoutError(
        f"run {run_id!r} was still being planned after {timeout:.0f}s. Another "
        "worker claimed it and has not finished writing its units — check whether "
        "that worker died, then re-run to take the plan over."
    )


def resume_run(state: StateStore, run_id: str) -> int:
    """Prepare a run to continue: clear any pause, return failed units to the
    pool, and reclaim units abandoned by crashed workers (expired leases).
    Returns how many units were re-queued (failed + reclaimed)."""
    state.set_run_state(run_id, RunState.RUNNING)
    failed = state.reset_failed_units(run_id)
    reclaimed = state.reclaim_expired_units(run_id)
    if failed or reclaimed:
        _log.info("run resumed", requeued_failed=failed, reclaimed_leases=reclaimed)
    return failed + reclaimed


def work_run(
    state: StateStore,
    *,
    run_id: str,
    source: DocumentSource,
    renderer: DocumentRenderer,
    blob: BlobStore,
) -> WorkerStats:
    """Drive a single worker over a run until its pool is empty.

    Multiple processes calling this against the same store is exactly the
    multi-worker case — the atomic claim keeps them from colliding. After the
    worker drains, the run is marked COMPLETED only if every unit is done;
    otherwise it is left RUNNING with failed units awaiting a resume.
    """
    bind(run_id=run_id)
    task_index = os.environ.get("CLOUD_RUN_TASK_INDEX") or os.environ.get(
        "AWS_BATCH_JOB_ARRAY_INDEX")
    if task_index is not None:
        bind(task=task_index)

    # Before anything is claimed: a source that cannot satisfy its run-scoped
    # configuration must stop the run here, not fail unit after unit.
    prepare_source(source, run_id)
    _log.info("worker started")
    worker = GenerationWorker(
        run_id=run_id, source=source, renderer=renderer, blob=blob, state=state
    )
    stats = worker.run()
    _log.info("worker finished", completed=stats.units_completed,
              failed=stats.units_failed, documents=stats.documents_written)

    progress = state.progress(run_id)
    outstanding = progress[WorkUnitState.PENDING] + progress[WorkUnitState.RUNNING]
    if outstanding == 0 and progress[WorkUnitState.FAILED] == 0:
        run = state.get_run(run_id)
        # Only on the transition to COMPLETED, not on every drain of an
        # already-finished run. That keeps the root manifest written exactly
        # once — so it is stable — and avoids a redundant state write each time a
        # worker drains a complete run.
        if run is not None and run.state is not RunState.COMPLETED:
            state.set_run_state(run_id, RunState.COMPLETED)
            _write_run_manifest(run, blob, source)
            _log.info("run completed", units=run.total_units,
                      documents=progress[WorkUnitState.DONE])
    elif progress[WorkUnitState.FAILED]:
        _log.warning("run left incomplete", failed=progress[WorkUnitState.FAILED],
                     pending=outstanding)
    return stats


def _write_run_manifest(run: Run, blob: BlobStore, source: DocumentSource) -> None:
    """Write the root manifest now the run is complete.

    Every unit part exists by here — a unit's part lands before it is marked
    done, and this runs only once no unit is outstanding. Gated on the
    COMPLETED transition by the caller, so it is written once per run; a
    simultaneous second completer would only re-assemble byte-identical
    substantive content from the same parts.
    """
    write_run_manifest(
        blob,
        run_id=run.run_id,
        pack=run.pack,
        config_id=run.config_id,
        total_units=run.total_units,
        # Which content pool produced the corpus — the same value that is on
        # every golden row, surfaced once at the run level for a consumer.
        catalogue_version=getattr(getattr(source, "_catalogue", None), "version", ""),
        created_at=run.created_at.isoformat(),
    )
