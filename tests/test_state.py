"""State-store tests. The atomic claim and resume semantics are the point."""

from __future__ import annotations

from pathlib import Path

import pytest

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state import Run, SqliteStateStore, WorkUnit, open_state


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
    with pytest.raises(ImportError, match=r"docloom\[gcp\]"):
        open_state("firestore://my-project/(default)")
