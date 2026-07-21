"""Run state — the record of what a run is doing and how far it has got.

Backs pause / resume / cancel and safe concurrency. A run is divided into work
units (one shard of documents each); workers *claim* units atomically, so the
same unit is never generated twice even when many workers pull from the queue
at once. That atomic claim is the whole reason this is a store with a protocol
and not a dict — SQLite provides it locally, Firestore provides it at scale, and
calling code sees the same interface.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from docloom.core.enums import RunState, WorkUnitState


def _now() -> datetime:
    return datetime.now(UTC)


#: Default lease lifetime. A unit's claim is valid for this long; if the worker
#: neither completes nor fails it within the window (a silent crash — spot/
#: preemptible termination, OOM, power loss), the unit is reclaimable. Chosen to
#: comfortably exceed the time to render one unit's worth of documents; too short
#: and a slow-but-alive worker's unit gets stolen, too long and recovery lags.
DEFAULT_LEASE_SECONDS: float = 900.0  # 15 minutes


@dataclass(frozen=True, slots=True)
class Run:
    """One generation run."""

    run_id: str
    pack: str                         # which DocumentPack produced it
    config_id: str                    # the run config this executed
    total_units: int
    state: RunState = RunState.PENDING
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One shard of a run — a contiguous range of document indices."""

    run_id: str
    unit_index: int
    start_index: int                  # first document index (inclusive)
    count: int                        # documents in this unit
    state: WorkUnitState = WorkUnitState.PENDING
    attempts: int = 0
    error: str | None = None
    updated_at: datetime = field(default_factory=_now)
    #: When the current claim expires. Set while ``running``, ``None`` otherwise.
    #: A ``running`` unit past this instant was abandoned by a crashed worker and
    #: is reclaimable — see :meth:`StateStore.reclaim_expired_units`.
    lease_expires_at: datetime | None = None

    @property
    def end_index(self) -> int:
        """One past the last document index in this unit."""
        return self.start_index + self.count

    def lease_is_expired(self, now: datetime) -> bool:
        """True for a ``running`` unit whose lease has lapsed as of ``now``."""
        return (
            self.state is WorkUnitState.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at <= now
        )


@runtime_checkable
class StateStore(Protocol):
    """Durable run + work-unit state with an atomic claim."""

    # ── Runs ────────────────────────────────────────────────────────────────
    def create_run(self, run: Run, units: list[WorkUnit]) -> None:
        """Persist a run and its units in one transaction.

        Atomic so a run is never half-created: either the whole plan is
        recorded or none of it is, and a resumed run always finds a complete
        unit list.
        """
        ...

    def get_run(self, run_id: str) -> Run | None:
        ...

    def set_run_state(self, run_id: str, state: RunState) -> None:
        ...

    # ── Work units ──────────────────────────────────────────────────────────
    def claim_next_unit(self, run_id: str) -> WorkUnit | None:
        """Atomically take the next **pending** unit and mark it running.

        Returns ``None`` when nothing is claimable (all done/failed, or the run
        is paused/cancelled). Concurrency-safe: two workers calling this at the
        same moment get different units or one gets ``None``.

        Only ``pending`` units are claimed — never ``failed``. A worker draining
        continuously must not re-claim a unit that just failed (it would retry
        it forever, since a failed unit has the lowest claimable index). Failed
        units re-enter the pool only on an explicit :meth:`reset_failed_units`,
        which resume calls — so a retry is a deliberate act, not an accidental
        hot loop.
        """
        ...

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        ...

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        """Mark a unit failed and record why. It stays failed — and out of the
        claimable pool — until :meth:`reset_failed_units` returns it, so the
        current worker moves on rather than immediately retrying."""
        ...

    def reset_failed_units(self, run_id: str) -> int:
        """Return every failed unit to ``pending``; returns how many. Called by
        resume, so a re-run retries what failed without re-doing what
        succeeded."""
        ...

    def reclaim_expired_units(self, run_id: str, *, now: datetime | None = None) -> int:
        """Return ``running`` units with an expired lease to ``pending``.

        The recovery path for a *silently crashed* worker: unlike
        :meth:`fail_unit` (an explicit failure), a worker that is killed
        mid-unit — spot/preemptible reclamation, OOM, power loss — leaves its
        unit ``running`` with no one to finish it. Its lease still lapses, and
        this returns it to the claimable pool. Returns how many were reclaimed.
        Called by resume and safe to call periodically while a run is active.
        """
        ...

    def units(self, run_id: str) -> Iterator[WorkUnit]:
        ...

    def progress(self, run_id: str) -> dict[WorkUnitState, int]:
        """Count units in each state — drives the run's completion check."""
        ...

    def close(self) -> None:
        ...
