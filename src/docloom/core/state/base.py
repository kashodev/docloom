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

    @property
    def end_index(self) -> int:
        """One past the last document index in this unit."""
        return self.start_index + self.count


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
        """Atomically take the next pending unit and mark it running.

        Returns ``None`` when nothing is claimable (all done, or the run is
        paused/cancelled). Concurrency-safe: two workers calling this at the
        same moment get different units or one gets ``None``.
        """
        ...

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        ...

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        """Mark a unit failed and record why. A failed unit returns to the
        claimable pool on resume so a transient error is retried rather than
        silently dropping its documents."""
        ...

    def units(self, run_id: str) -> Iterator[WorkUnit]:
        ...

    def progress(self, run_id: str) -> dict[WorkUnitState, int]:
        """Count units in each state — drives the run's completion check."""
        ...

    def close(self) -> None:
        ...
