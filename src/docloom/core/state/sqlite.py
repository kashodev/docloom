"""SQLite state store — the local default.

A single file, no server, WAL mode for concurrent readers. The claim is atomic
via ``BEGIN IMMEDIATE`` plus ``UPDATE … RETURNING``: the immediate transaction
takes a write lock before selecting, so two workers can never claim the same
unit. That is enough for local runs and small deployments; the Cloud Run +
Firestore path (phase 4) uses the same protocol for high concurrency.

A resumed run reclaims both ``pending`` and ``failed`` units, so pause/resume
loses no work and a transient failure is retried rather than dropped.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import Run, WorkUnit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    pack         TEXT NOT NULL,
    config_id    TEXT NOT NULL,
    total_units  INTEGER NOT NULL,
    state        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS work_units (
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    unit_index   INTEGER NOT NULL,
    start_index  INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, unit_index)
);
CREATE INDEX IF NOT EXISTS idx_units_claimable
    ON work_units(run_id, state, unit_index);
"""

_CLAIMABLE = (WorkUnitState.PENDING.value, WorkUnitState.FAILED.value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteStateStore:
    """A :class:`~docloom.core.state.base.StateStore` in one SQLite file."""

    scheme = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # ── Runs ────────────────────────────────────────────────────────────────
    def create_run(self, run: Run, units: list[WorkUnit]) -> None:
        with self._conn:  # single transaction: run + all its units, or nothing
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO runs (run_id, pack, config_id, total_units, state, "
                "created_at, updated_at, metadata) VALUES (?,?,?,?,?,?,?,?)",
                (run.run_id, run.pack, run.config_id, run.total_units, run.state.value,
                 run.created_at.isoformat(), run.updated_at.isoformat(),
                 json.dumps(run.metadata)),
            )
            self._conn.executemany(
                "INSERT INTO work_units (run_id, unit_index, start_index, count, "
                "state, attempts, error, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [(u.run_id, u.unit_index, u.start_index, u.count, u.state.value,
                  u.attempts, u.error, u.updated_at.isoformat()) for u in units],
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._to_run(row) if row else None

    def set_run_state(self, run_id: str, state: RunState) -> None:
        self._conn.execute(
            "UPDATE runs SET state=?, updated_at=? WHERE run_id=?",
            (state.value, _now(), run_id),
        )

    # ── Work units ──────────────────────────────────────────────────────────
    def claim_next_unit(self, run_id: str) -> WorkUnit | None:
        run = self.get_run(run_id)
        if run is None or run.state in (RunState.PAUSED, RunState.CANCELLED):
            return None

        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")   # take the write lock first
            row = self._conn.execute(
                "SELECT unit_index FROM work_units "
                "WHERE run_id=? AND state IN (?, ?) ORDER BY unit_index LIMIT 1",
                (run_id, *_CLAIMABLE),
            ).fetchone()
            if row is None:
                return None
            claimed = self._conn.execute(
                "UPDATE work_units SET state=?, attempts=attempts+1, updated_at=? "
                "WHERE run_id=? AND unit_index=? RETURNING *",
                (WorkUnitState.RUNNING.value, _now(), run_id, row["unit_index"]),
            ).fetchone()
        return self._to_unit(claimed)

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        self._conn.execute(
            "UPDATE work_units SET state=?, error=NULL, updated_at=? "
            "WHERE run_id=? AND unit_index=?",
            (WorkUnitState.DONE.value, _now(), run_id, unit_index),
        )

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        self._conn.execute(
            "UPDATE work_units SET state=?, error=?, updated_at=? "
            "WHERE run_id=? AND unit_index=?",
            (WorkUnitState.FAILED.value, error, _now(), run_id, unit_index),
        )

    def units(self, run_id: str) -> Iterator[WorkUnit]:
        rows = self._conn.execute(
            "SELECT * FROM work_units WHERE run_id=? ORDER BY unit_index", (run_id,)
        ).fetchall()
        return iter([self._to_unit(r) for r in rows])

    def progress(self, run_id: str) -> dict[WorkUnitState, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) n FROM work_units WHERE run_id=? GROUP BY state",
            (run_id,),
        ).fetchall()
        counts = {state: 0 for state in WorkUnitState}
        for row in rows:
            counts[WorkUnitState(row["state"])] = row["n"]
        return counts

    def close(self) -> None:
        self._conn.close()

    # ── Row mapping ─────────────────────────────────────────────────────────
    @staticmethod
    def _to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"], pack=row["pack"], config_id=row["config_id"],
            total_units=row["total_units"], state=RunState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _to_unit(row: sqlite3.Row) -> WorkUnit:
        return WorkUnit(
            run_id=row["run_id"], unit_index=row["unit_index"],
            start_index=row["start_index"], count=row["count"],
            state=WorkUnitState(row["state"]), attempts=row["attempts"],
            error=row["error"], updated_at=datetime.fromisoformat(row["updated_at"]),
        )
