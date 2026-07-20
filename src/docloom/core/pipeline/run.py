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

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.pipeline.planner import plan_units
from docloom.core.pipeline.renderer import DocumentRenderer
from docloom.core.pipeline.source import DocumentSource
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
) -> Run:
    """Plan a run into units and record it, ready to be worked.

    One transaction records the run and every unit, so a resumed run always
    finds a complete plan.
    """
    units = plan_units(run_id, total, unit_size)
    run = Run(run_id=run_id, pack=pack, config_id=config_id, total_units=len(units),
              state=RunState.RUNNING)
    state.create_run(run, units)
    return run


def resume_run(state: StateStore, run_id: str) -> int:
    """Prepare a run to continue: clear any pause and return failed units to the
    pool. Returns how many failed units were re-queued."""
    state.set_run_state(run_id, RunState.RUNNING)
    return state.reset_failed_units(run_id)


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
    worker = GenerationWorker(
        run_id=run_id, source=source, renderer=renderer, blob=blob, state=state
    )
    stats = worker.run()

    progress = state.progress(run_id)
    outstanding = progress[WorkUnitState.PENDING] + progress[WorkUnitState.RUNNING]
    if outstanding == 0 and progress[WorkUnitState.FAILED] == 0:
        state.set_run_state(run_id, RunState.COMPLETED)
    return stats
