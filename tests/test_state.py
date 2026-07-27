"""State-store tests. The atomic claim and resume semantics are the point."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from docsynth.core.enums import RunState, WorkUnitState
from docsynth.core.pipeline.run import resume_run
from docsynth.core.state import Run, SqliteStateStore, WorkUnit, open_state


def make_run(store: SqliteStateStore, run_id: str = "run_1", units: int = 4) -> Run:
    run = Run(run_id=run_id, pack="invoice", config_id="cfg_1", total_units=units)
    work = [
        WorkUnit(run_id=run_id, unit_index=i, start_index=i * 1000, count=1000)
        for i in range(units)
    ]
    store.create_run(run, work)
    return run


def test_create_and_get_run(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store)
    run = store.get_run("run_1")
    assert run is not None
    assert run.pack == "invoice"
    assert run.total_units == 4


def test_claim_walks_units_in_order_then_returns_none(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, units=3)
    claimed = [store.claim_next_unit("run_1") for _ in range(4)]
    assert [u.unit_index for u in claimed[:3]] == [0, 1, 2]  # type: ignore[union-attr]
    assert claimed[3] is None
    assert all(u.state is WorkUnitState.RUNNING for u in claimed[:3])  # type: ignore[union-attr]


def test_claimed_unit_carries_its_index_range(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store)
    unit = store.claim_next_unit("run_1")
    assert unit is not None
    assert unit.start_index == 0
    assert unit.end_index == 1000


def test_a_unit_is_never_claimed_twice(tmp_path: Path) -> None:
    """The core concurrency guarantee. Simulated by interleaving claims — the
    same index must not come back twice."""
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, units=5)
    seen = []
    while (u := store.claim_next_unit("run_1")) is not None:
        seen.append(u.unit_index)
    assert sorted(seen) == [0, 1, 2, 3, 4]
    assert len(seen) == len(set(seen))


def test_complete_and_fail_update_progress(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, units=3)
    for _ in range(3):
        store.claim_next_unit("run_1")
    store.complete_unit("run_1", 0)
    store.fail_unit("run_1", 1, "boom")
    progress = store.progress("run_1")
    assert progress[WorkUnitState.DONE] == 1
    assert progress[WorkUnitState.FAILED] == 1
    assert progress[WorkUnitState.RUNNING] == 1


def test_failed_units_are_not_claimed_until_reset(tmp_path: Path) -> None:
    """A failed unit stays out of the pool so a draining worker moves on rather
    than hot-looping on it. Resume calls reset_failed_units to retry it."""
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, units=2)
    u = store.claim_next_unit("run_1")           # unit 0
    assert u is not None and u.unit_index == 0
    store.fail_unit("run_1", 0, "network blip")

    # Draining continues past the failure to the next pending unit, never back
    # to the failed one.
    nxt = store.claim_next_unit("run_1")
    assert nxt is not None and nxt.unit_index == 1
    store.complete_unit("run_1", 1)
    assert store.claim_next_unit("run_1") is None   # failed unit is NOT reclaimed

    # Resume: reset returns the failed unit to the pool, attempts preserved.
    assert store.reset_failed_units("run_1") == 1
    reclaimed = store.claim_next_unit("run_1")
    assert reclaimed is not None
    assert reclaimed.unit_index == 0
    assert reclaimed.attempts == 2                   # 1 from first claim + 1 now


def test_reset_failed_units_returns_zero_when_none_failed(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, units=2)
    assert store.reset_failed_units("run_1") == 0


# ── Lease + reclaim (crashed-worker recovery) ───────────────────────────────
def test_claim_stamps_a_future_lease(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=900)
    make_run(store, units=1)
    unit = store.claim_next_unit("run_1")
    assert unit is not None and unit.lease_expires_at is not None
    assert unit.lease_expires_at > datetime.now(UTC)


def test_reclaim_returns_expired_running_units_to_the_pool(tmp_path: Path) -> None:
    """A silently crashed worker (unit left running, lease lapsed) is recovered."""
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=900)
    make_run(store, units=2)
    crashed = store.claim_next_unit("run_1")            # unit 0, never completed
    assert crashed is not None and crashed.unit_index == 0

    # Nothing is expired yet, so a reclaim at 'now' is a no-op.
    assert store.reclaim_expired_units("run_1") == 0

    # Well past the lease, the abandoned unit is reclaimed exactly once...
    future = datetime.now(UTC) + timedelta(hours=1)
    assert store.reclaim_expired_units("run_1", now=future) == 1
    assert store.reclaim_expired_units("run_1", now=future) == 0   # idempotent

    # ...and is claimable again, attempts preserved across the reclaim.
    again = store.claim_next_unit("run_1")
    assert again is not None and again.unit_index == 0
    assert again.attempts == 2


def test_reclaim_leaves_live_leases_alone(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=900)
    make_run(store, units=1)
    store.claim_next_unit("run_1")
    assert store.reclaim_expired_units("run_1") == 0             # lease still valid
    assert store.progress("run_1")[WorkUnitState.RUNNING] == 1


def test_completed_and_failed_units_clear_their_lease(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=900)
    make_run(store, units=2)
    store.claim_next_unit("run_1")
    store.claim_next_unit("run_1")
    store.complete_unit("run_1", 0)
    store.fail_unit("run_1", 1, "boom")
    by_index = {u.unit_index: u for u in store.units("run_1")}
    assert by_index[0].lease_expires_at is None
    assert by_index[1].lease_expires_at is None
    # A cleared lease is never reclaimed, even far in the future.
    assert store.reclaim_expired_units("run_1", now=datetime.now(UTC) + timedelta(days=1)) == 0


def test_claim_opportunistically_reclaims_abandoned_units(tmp_path: Path) -> None:
    """A continuously draining fleet recovers a crashed worker's unit on the next
    claim — no explicit resume needed. lease_seconds=0 expires the lease at once."""
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=0)
    make_run(store, units=1)
    first = store.claim_next_unit("run_1")               # unit 0, lease already lapsed
    assert first is not None and first.attempts == 1
    # The worker "crashes" (never completes). The next claim reclaims and re-serves it.
    again = store.claim_next_unit("run_1")
    assert again is not None and again.unit_index == 0
    assert again.attempts == 2


def test_resume_reclaims_crashed_units_and_reports_the_count(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db", lease_seconds=0)
    make_run(store, units=3)
    store.claim_next_unit("run_1")                       # unit 0 — will be "crashed"
    store.set_run_state("run_1", RunState.PAUSED)
    # unit 0 is running with a lapsed lease; resume reclaims it.
    requeued = resume_run(store, "run_1")
    assert requeued == 1
    assert store.progress("run_1")[WorkUnitState.RUNNING] == 0
    assert store.progress("run_1")[WorkUnitState.PENDING] == 3


def test_old_db_without_lease_column_is_migrated(tmp_path: Path) -> None:
    """Opening a pre-lease runs.db must not fail — the column is added in place."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, pack TEXT, config_id TEXT, "
        "total_units INT, state TEXT, created_at TEXT, updated_at TEXT, metadata TEXT);"
        "CREATE TABLE work_units (run_id TEXT, unit_index INT, start_index INT, count INT, "
        "state TEXT, attempts INT DEFAULT 0, error TEXT, updated_at TEXT, "
        "PRIMARY KEY (run_id, unit_index));"
    )
    conn.close()
    store = SqliteStateStore(db)                          # must migrate, not raise
    make_run(store, units=1)
    unit = store.claim_next_unit("run_1")
    assert unit is not None and unit.lease_expires_at is not None


def test_paused_run_yields_no_claims(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store)
    store.set_run_state("run_1", RunState.PAUSED)
    assert store.claim_next_unit("run_1") is None
    store.set_run_state("run_1", RunState.RUNNING)
    assert store.claim_next_unit("run_1") is not None


def test_cancelled_run_yields_no_claims(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store)
    store.set_run_state("run_1", RunState.CANCELLED)
    assert store.claim_next_unit("run_1") is None


def test_state_survives_reopen(tmp_path: Path) -> None:
    """Resume after a process restart reads a complete plan from disk."""
    db = tmp_path / "s.db"
    store = SqliteStateStore(db)
    make_run(store, units=3)
    store.claim_next_unit("run_1")
    store.complete_unit("run_1", 0)
    store.close()

    reopened = SqliteStateStore(db)
    assert reopened.progress("run_1")[WorkUnitState.DONE] == 1
    assert [u.unit_index for u in reopened.units("run_1")] == [0, 1, 2]


def test_factory_defaults_to_sqlite(tmp_path: Path) -> None:
    assert isinstance(open_state(str(tmp_path / "runs.db")), SqliteStateStore)


def test_factory_firestore_needs_gcp_extra() -> None:
    with pytest.raises(ImportError, match=r"docsynth\[gcp\]"):
        open_state("firestore://my-project/(default)")


def test_factory_dynamodb_needs_aws_extra() -> None:
    with pytest.raises(ImportError, match=r"docsynth\[aws\]"):
        open_state("dynamodb://docsynth-state")


def test_factory_rejects_an_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="unsupported state scheme"):
        open_state("redis://localhost/0")


def test_quarantine_set_unions_and_persists(tmp_path: Path) -> None:
    """Providers quarantined by one unit are visible to later units, and the
    union is idempotent — the core of cross-unit quarantine persistence (R1)."""
    db = tmp_path / "s.db"
    store = SqliteStateStore(db)
    make_run(store, "b")
    assert store.quarantined_providers("b") == set()

    store.quarantine_providers("b", {"deepseek"})
    store.quarantine_providers("b", {"deepseek", "dashscope"})   # union, dup ok
    assert store.quarantined_providers("b") == {"deepseek", "dashscope"}
    store.quarantine_providers("b", set())                       # no-op
    store.close()

    reopened = SqliteStateStore(db)                              # survives a restart
    assert reopened.quarantined_providers("b") == {"deepseek", "dashscope"}


def test_quarantine_sets_are_per_run(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.db")
    make_run(store, "b1")
    make_run(store, "b2")
    store.quarantine_providers("b1", {"deepseek"})
    assert store.quarantined_providers("b2") == set()
