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

import time

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.pipeline.planner import plan_units
from docloom.core.pipeline.renderer import DocumentRenderer
from docloom.core.pipeline.source import DocumentSource, prepare_source
from docloom.core.pipeline.worker import GenerationWorker, WorkerStats
from docloom.core.state.base import Run, StateStore
from docloom.core.storage.base import BlobStore


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
    if not state.create_run(run, units):
        # Another worker won the race to plan this run. It may still be writing
        # units, and claiming from a half-written plan would look like an empty
        # run, so wait for it to finish rather than racing ahead.
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
    requeued = state.reset_failed_units(run_id)
    requeued += state.reclaim_expired_units(run_id)
    return requeued


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
    # Before anything is claimed: a source that cannot satisfy its run-scoped
    # configuration must stop the run here, not fail unit after unit.
    prepare_source(source, run_id)
    worker = GenerationWorker(
        run_id=run_id, source=source, renderer=renderer, blob=blob, state=state
    )
    stats = worker.run()

    progress = state.progress(run_id)
    outstanding = progress[WorkUnitState.PENDING] + progress[WorkUnitState.RUNNING]
    if outstanding == 0 and progress[WorkUnitState.FAILED] == 0:
        state.set_run_state(run_id, RunState.COMPLETED)
    return stats
