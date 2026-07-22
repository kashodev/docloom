"""Run-planning tests — the cold-start race and its recovery.

Every worker starts by checking whether its run exists, so N workers starting
together all see "no run" and all try to plan it. The later ones used to
overwrite units the earlier ones had already claimed, so the same documents were
generated twice; on SQLite the second planner died on a UNIQUE constraint
instead. Creation is now a conditional write — the same primitive the unit claim
uses — so exactly one caller plans and the rest are told so.

The load-bearing tests here are that a losing planner **cannot reset a claimed
unit**, and that a half-written plan is never mistaken for a finished run.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.pipeline.run import create_run as plan_and_record
from docloom.core.state import SqliteStateStore
from docloom.core.state.base import Run, WorkUnit


def store(tmp_path: Path, name: str = "s.db") -> SqliteStateStore:
    return SqliteStateStore(tmp_path / name)


def a_plan(run_id: str = "r", units: int = 4) -> tuple[Run, list[WorkUnit]]:
    run = Run(run_id=run_id, pack="invoice", config_id="cfg", total_units=units,
              state=RunState.RUNNING)
    work = [WorkUnit(run_id=run_id, unit_index=i, start_index=i * 100, count=100)
            for i in range(units)]
    return run, work


# ── Exactly one planner ─────────────────────────────────────────────────────
def test_the_first_caller_plans_and_the_second_is_told_it_lost(tmp_path: Path) -> None:
    s = store(tmp_path)
    run, units = a_plan()
    assert s.create_run(run, units) is True
    assert s.create_run(run, units) is False


def test_losing_does_not_raise(tmp_path: Path) -> None:
    """Regression: a second concurrent process used to hit
    `IntegrityError: UNIQUE constraint failed: runs.run_id` and die."""
    s = store(tmp_path)
    run, units = a_plan()
    s.create_run(run, units)
    s.create_run(run, units)          # must not raise


def test_a_late_planner_cannot_reset_a_claimed_unit(tmp_path: Path) -> None:
    """The actual damage the race caused: worker B replans, unit 0 goes back to
    pending, and a second worker regenerates documents worker A is already
    producing."""
    s = store(tmp_path)
    run, units = a_plan()
    s.create_run(run, units)

    claimed = s.claim_next_unit("r")                 # worker A takes unit 0
    assert claimed is not None and claimed.unit_index == 0

    assert s.create_run(run, units) is False         # worker B starts late

    still = {u.unit_index: u for u in s.units("r")}[0]
    assert still.state is WorkUnitState.RUNNING, "a losing planner reset a claimed unit"
    assert still.attempts == 1


def test_only_one_of_many_concurrent_planners_wins(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    SqliteStateStore(db).close()
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        s = SqliteStateStore(db)
        run, units = a_plan(units=8)
        won = s.create_run(run, units)
        with lock:
            results.append(won)
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"{sum(results)} planners won, expected exactly 1"
    # And the plan is intact: 8 units, none duplicated.
    final = SqliteStateStore(db)
    assert [u.unit_index for u in final.units("r")] == list(range(8))


def test_the_plan_is_complete_when_the_winner_returns(tmp_path: Path) -> None:
    s = store(tmp_path)
    run, units = a_plan(units=5)
    s.create_run(run, units)
    assert s.get_run("r").planned is True           # type: ignore[union-attr]
    assert len(list(s.units("r"))) == 5


# ── A half-written plan is not a finished run ───────────────────────────────
def test_an_unplanned_run_yields_no_claims(tmp_path: Path) -> None:
    """A run whose units are still being written has nothing to claim *yet*.
    Treating that as nothing-to-claim-ever would send every worker home from a
    run that had not started."""
    s = store(tmp_path)
    run, units = a_plan()
    s.create_run(run, units)
    # Force the half-written state SQLite cannot reach on its own (it writes the
    # marker and units in one transaction) to exercise the guard every backend
    # shares.
    s._conn.execute("UPDATE runs SET planned=0 WHERE run_id='r'")
    assert s.claim_next_unit("r") is None

    s._conn.execute("UPDATE runs SET planned=1 WHERE run_id='r'")
    assert s.claim_next_unit("r") is not None


def test_sqlite_never_exposes_an_unplanned_run(tmp_path: Path) -> None:
    """Marker and units land in one transaction, so the intermediate state that
    Firestore and DynamoDB must guard against cannot occur here."""
    s = store(tmp_path)
    run, units = a_plan()
    s.create_run(run, units)
    assert s.get_run("r").planned is True           # type: ignore[union-attr]


# ── Backwards compatibility ─────────────────────────────────────────────────
def test_a_run_written_before_the_flag_reads_as_planned(tmp_path: Path) -> None:
    """The old path only ever wrote a run whose units were already committed, so
    absent means planned. Defaulting the other way would strand every existing
    run."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, pack TEXT, config_id TEXT, "
        "total_units INT, state TEXT, created_at TEXT, updated_at TEXT, metadata TEXT);"
        "CREATE TABLE work_units (run_id TEXT, unit_index INT, start_index INT, count INT, "
        "state TEXT, attempts INT DEFAULT 0, error TEXT, updated_at TEXT, "
        "PRIMARY KEY (run_id, unit_index));"
        "INSERT INTO runs VALUES ('old','invoice','cfg',1,'running',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','{}');"
        "INSERT INTO work_units VALUES ('old',0,0,100,'pending',0,NULL,"
        "'2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    s = SqliteStateStore(db)                        # migrates in place
    run = s.get_run("old")
    assert run is not None and run.planned is True
    assert s.claim_next_unit("old") is not None     # still workable


# ── Through the pipeline ────────────────────────────────────────────────────
def test_pipeline_create_run_is_safe_from_every_worker(tmp_path: Path) -> None:
    s = store(tmp_path)
    first = plan_and_record(s, run_id="r", pack="invoice", config_id="c",
                            total=400, unit_size=100)
    second = plan_and_record(s, run_id="r", pack="invoice", config_id="c",
                             total=400, unit_size=100)
    assert first.total_units == second.total_units == 4
    assert len(list(s.units("r"))) == 4


def test_pipeline_waits_for_another_workers_plan(tmp_path: Path) -> None:
    """A loser must not race ahead into a half-written plan."""
    s = store(tmp_path)
    run, units = a_plan(units=3)
    s.create_run(run, units)
    s._conn.execute("UPDATE runs SET planned=0 WHERE run_id='r'")

    ready = threading.Event()

    def finish_planning() -> None:
        ready.wait(timeout=5)
        SqliteStateStore(tmp_path / "s.db")._conn.execute(
            "UPDATE runs SET planned=1 WHERE run_id='r'"
        )

    finisher = threading.Thread(target=finish_planning)
    finisher.start()
    ready.set()
    plan_and_record(s, run_id="r", pack="invoice", config_id="c",
                    total=300, unit_size=100, wait_timeout=10)
    finisher.join()
    assert s.get_run("r").planned is True            # type: ignore[union-attr]


def test_pipeline_raises_rather_than_silently_producing_nothing(tmp_path: Path) -> None:
    """A worker that gives up on an unplanned run would claim nothing, exit 0 and
    report a finished run that generated no documents — the failure hardest to
    notice. It must be loud."""
    s = store(tmp_path)
    run, units = a_plan()
    s.create_run(run, units)
    s._conn.execute("UPDATE runs SET planned=0 WHERE run_id='r'")

    with pytest.raises(TimeoutError, match="still being planned"):
        plan_and_record(s, run_id="r", pack="invoice", config_id="c",
                        total=400, unit_size=100, wait_timeout=1.0)
