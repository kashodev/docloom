"""SQLite state store — the local default.

A single file, no server, WAL mode for concurrent readers. The claim is atomic
via ``BEGIN IMMEDIATE`` plus ``UPDATE … RETURNING``: the immediate transaction
takes a write lock before selecting, so two workers can never claim the same
unit. That is enough for local runs and small deployments; the Cloud Run +
Firestore path (phase 4) uses the same protocol for high concurrency.

The claim takes ``pending`` units only. Resume calls :meth:`reset_failed_units`
first, which returns failed units to ``pending`` — so a re-run retries what
failed without a single worker hot-looping on a unit that keeps failing.

Each claim also stamps a **lease** (``lease_expires_at``) and, under the same
write lock, reclaims any ``running`` unit whose lease has lapsed — the recovery
path for a worker killed mid-unit (spot/preemptible termination). The reclaim is
a cheap indexed ``UPDATE`` inside the lock the claim already holds, so a
continuously draining fleet self-heals without an explicit resume.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import (
    DEFAULT_LEASE_SECONDS,
    TOTAL_MODEL,
    Run,
    Spend,
    WorkUnit,
    from_nano,
    to_nano,
)

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
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    unit_index       INTEGER NOT NULL,
    start_index      INTEGER NOT NULL,
    count            INTEGER NOT NULL,
    state            TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    updated_at       TEXT NOT NULL,
    lease_expires_at TEXT,
    PRIMARY KEY (run_id, unit_index)
);
CREATE INDEX IF NOT EXISTS idx_units_claimable
    ON work_units(run_id, state, unit_index);
-- Spend rollup. Nano-dollars as INTEGER so the increment is atomic and exact;
-- see docloom.core.state.base for why not a float or a decimal string.
CREATE TABLE IF NOT EXISTS run_spend (
    run_id        TEXT NOT NULL,
    model         TEXT NOT NULL,
    cost_nano     INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, model)
);
"""

_CLAIMABLE = (WorkUnitState.PENDING.value,)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _now_dt() -> datetime:
    return datetime.now(UTC)


class SqliteStateStore:
    """A :class:`~docloom.core.state.base.StateStore` in one SQLite file."""

    scheme = "sqlite"

    def __init__(self, path: str | Path, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self._lease_seconds = lease_seconds
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a pre-existing DB may lack, so an older runs.db opens."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(work_units)")}
        if "lease_expires_at" not in cols:
            self._conn.execute("ALTER TABLE work_units ADD COLUMN lease_expires_at TEXT")

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
                "state, attempts, error, updated_at, lease_expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(u.run_id, u.unit_index, u.start_index, u.count, u.state.value,
                  u.attempts, u.error, u.updated_at.isoformat(),
                  u.lease_expires_at.isoformat() if u.lease_expires_at else None)
                 for u in units],
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
            # Opportunistic reclaim: a unit abandoned by a crashed worker (running
            # with a lapsed lease) rejoins the pool here, so a continuously
            # draining fleet recovers it without waiting for an explicit resume.
            # Cheap — an indexed UPDATE inside the lock we already hold.
            now = _now_dt()
            self._reclaim_expired(now, run_id=run_id)
            row = self._conn.execute(
                "SELECT unit_index FROM work_units "
                "WHERE run_id=? AND state=? ORDER BY unit_index LIMIT 1",
                (run_id, *_CLAIMABLE),
            ).fetchone()
            if row is None:
                return None
            lease = (now + timedelta(seconds=self._lease_seconds)).isoformat()
            claimed = self._conn.execute(
                "UPDATE work_units SET state=?, attempts=attempts+1, updated_at=?, "
                "lease_expires_at=? WHERE run_id=? AND unit_index=? RETURNING *",
                (WorkUnitState.RUNNING.value, now.isoformat(), lease,
                 run_id, row["unit_index"]),
            ).fetchone()
        return self._to_unit(claimed)

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        # Clear the lease: a terminal unit is no longer held by any worker.
        self._conn.execute(
            "UPDATE work_units SET state=?, error=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE run_id=? AND unit_index=?",
            (WorkUnitState.DONE.value, _now(), run_id, unit_index),
        )

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        self._conn.execute(
            "UPDATE work_units SET state=?, error=?, lease_expires_at=NULL, updated_at=? "
            "WHERE run_id=? AND unit_index=?",
            (WorkUnitState.FAILED.value, error, _now(), run_id, unit_index),
        )

    def reset_failed_units(self, run_id: str) -> int:
        cur = self._conn.execute(
            "UPDATE work_units SET state=?, error=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE run_id=? AND state=?",
            (WorkUnitState.PENDING.value, _now(), run_id, WorkUnitState.FAILED.value),
        )
        return cur.rowcount

    def reclaim_expired_units(self, run_id: str, *, now: datetime | None = None) -> int:
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            return self._reclaim_expired(now or _now_dt(), run_id=run_id)

    def _reclaim_expired(self, now: datetime, *, run_id: str | None = None) -> int:
        """Running units with a lapsed lease -> pending. Assumes a write lock is
        already held (called inside claim's transaction and by the public method).
        Scoped to ``run_id`` when given, else across all runs."""
        sql = (
            "UPDATE work_units SET state=?, lease_expires_at=NULL, updated_at=? "
            "WHERE state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?"
        )
        params: list[object] = [
            WorkUnitState.PENDING.value, now.isoformat(),
            WorkUnitState.RUNNING.value, now.isoformat(),
        ]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        return self._conn.execute(sql, params).rowcount

    # ── Spend rollup ────────────────────────────────────────────────────────
    def add_spend(
        self,
        run_id: str,
        model: str,
        *,
        cost: Decimal,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Decimal:
        """Increment the model row and the run total in one transaction.

        ``BEGIN IMMEDIATE`` takes the write lock before either upsert, so
        concurrent workers serialise and the returned total is the value *after*
        this contribution — which is what a budget check needs.
        """
        nano = to_nano(cost)
        now = _now()
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            for key in (model, TOTAL_MODEL):
                self._conn.execute(
                    "INSERT INTO run_spend "
                    "(run_id, model, cost_nano, calls, input_tokens, output_tokens, updated_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id, model) DO UPDATE SET "
                    "  cost_nano = cost_nano + excluded.cost_nano,"
                    "  calls = calls + excluded.calls,"
                    "  input_tokens = input_tokens + excluded.input_tokens,"
                    "  output_tokens = output_tokens + excluded.output_tokens,"
                    "  updated_at = excluded.updated_at",
                    (run_id, key, nano, calls, input_tokens, output_tokens, now),
                )
            row = self._conn.execute(
                "SELECT cost_nano FROM run_spend WHERE run_id=? AND model=?",
                (run_id, TOTAL_MODEL),
            ).fetchone()
        return from_nano(int(row["cost_nano"]))

    def spend(self, run_id: str) -> list[Spend]:
        rows = self._conn.execute(
            "SELECT * FROM run_spend WHERE run_id=? ORDER BY model", (run_id,)
        ).fetchall()
        return [
            Spend(
                run_id=r["run_id"], model=r["model"],
                cost_usd=from_nano(int(r["cost_nano"])), calls=int(r["calls"]),
                input_tokens=int(r["input_tokens"]), output_tokens=int(r["output_tokens"]),
            )
            for r in rows
        ]

    def total_spend(self, run_id: str) -> Decimal:
        row = self._conn.execute(
            "SELECT cost_nano FROM run_spend WHERE run_id=? AND model=?",
            (run_id, TOTAL_MODEL),
        ).fetchone()
        return from_nano(int(row["cost_nano"])) if row else Decimal(0)

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
        lease = row["lease_expires_at"]
        return WorkUnit(
            run_id=row["run_id"], unit_index=row["unit_index"],
            start_index=row["start_index"], count=row["count"],
            state=WorkUnitState(row["state"]), attempts=row["attempts"],
            error=row["error"], updated_at=datetime.fromisoformat(row["updated_at"]),
            lease_expires_at=datetime.fromisoformat(lease) if lease else None,
        )
