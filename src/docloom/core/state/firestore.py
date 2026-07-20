"""Firestore state store (``firestore://project/database``).

The scale path for the atomic claim. Where SQLite serialises workers with a
local write lock, Firestore serialises them with a document transaction — so
the Cloud Run generation Service can run many concurrent instances against one
run and still never generate the same shard twice.

Layout: one document per run in ``runs``, with a ``work_units`` subcollection.

**Testing note.** Distributed-transaction correctness is Firestore's guarantee,
not something a dict-backed fake can prove, so this module does not pretend to
unit-test it. The error-prone parts — the record↔document mapping and the
claimable-unit selection — are pure module functions, tested directly. The real
claim is exercised by an emulator-gated integration test (skipped unless
``FIRESTORE_EMULATOR_HOST`` is set).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import Run, WorkUnit

_CLAIMABLE = (WorkUnitState.PENDING.value, WorkUnitState.FAILED.value)


# ── Pure mapping (unit-tested without the SDK) ──────────────────────────────
def run_to_doc(run: Run) -> dict[str, Any]:
    return {
        "pack": run.pack,
        "config_id": run.config_id,
        "total_units": run.total_units,
        "state": run.state.value,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "metadata": run.metadata,
    }


def doc_to_run(run_id: str, doc: dict[str, Any]) -> Run:
    return Run(
        run_id=run_id,
        pack=doc["pack"],
        config_id=doc["config_id"],
        total_units=doc["total_units"],
        state=RunState(doc["state"]),
        created_at=datetime.fromisoformat(doc["created_at"]),
        updated_at=datetime.fromisoformat(doc["updated_at"]),
        metadata=doc.get("metadata", {}),
    )


def unit_to_doc(unit: WorkUnit) -> dict[str, Any]:
    return {
        "unit_index": unit.unit_index,
        "start_index": unit.start_index,
        "count": unit.count,
        "state": unit.state.value,
        "attempts": unit.attempts,
        "error": unit.error,
        "updated_at": unit.updated_at.isoformat(),
    }


def doc_to_unit(run_id: str, doc: dict[str, Any]) -> WorkUnit:
    return WorkUnit(
        run_id=run_id,
        unit_index=doc["unit_index"],
        start_index=doc["start_index"],
        count=doc["count"],
        state=WorkUnitState(doc["state"]),
        attempts=doc.get("attempts", 0),
        error=doc.get("error"),
        updated_at=datetime.fromisoformat(doc["updated_at"]),
    )


def run_is_claimable(run: Run | None) -> bool:
    """A run yields claims only while it is neither paused nor cancelled."""
    return run is not None and run.state not in (RunState.PAUSED, RunState.CANCELLED)


# ── Adapter ─────────────────────────────────────────────────────────────────
class FirestoreStateStore:
    """A :class:`~docloom.core.state.base.StateStore` backed by Firestore."""

    scheme = "firestore"

    def __init__(self, project: str, database: str = "(default)", *, client: Any | None = None) -> None:
        if client is None:
            try:
                from google.cloud import firestore
            except ImportError as exc:
                raise ImportError(
                    "firestore:// state needs the GCP extra — pip install 'docloom[gcp]'"
                ) from exc
            client = firestore.Client(project=project, database=database)
        self._client = client

    def _run_ref(self, run_id: str) -> Any:
        return self._client.collection("runs").document(run_id)

    def _units_col(self, run_id: str) -> Any:
        return self._run_ref(run_id).collection("work_units")

    def create_run(self, run: Run, units: list[WorkUnit]) -> None:
        # A batched write so the run and its units land together; a resumed run
        # never finds a half-created plan.
        batch = self._client.batch()
        batch.set(self._run_ref(run.run_id), run_to_doc(run))
        units_col = self._units_col(run.run_id)
        for unit in units:
            batch.set(units_col.document(str(unit.unit_index)), unit_to_doc(unit))
        batch.commit()

    def get_run(self, run_id: str) -> Run | None:
        snap = self._run_ref(run_id).get()
        return doc_to_run(run_id, snap.to_dict()) if snap.exists else None

    def set_run_state(self, run_id: str, state: RunState) -> None:
        self._run_ref(run_id).update({"state": state.value, "updated_at": _now()})

    def claim_next_unit(self, run_id: str) -> WorkUnit | None:
        if not run_is_claimable(self.get_run(run_id)):
            return None
        from google.cloud import firestore

        transaction = self._client.transaction()
        return _claim_in_transaction(transaction, self._units_col(run_id), run_id)

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        self._units_col(run_id).document(str(unit_index)).update(
            {"state": WorkUnitState.DONE.value, "error": None, "updated_at": _now()}
        )

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        self._units_col(run_id).document(str(unit_index)).update(
            {"state": WorkUnitState.FAILED.value, "error": error, "updated_at": _now()}
        )

    def units(self, run_id: str) -> Iterator[WorkUnit]:
        snaps = self._units_col(run_id).order_by("unit_index").stream()
        return iter([doc_to_unit(run_id, s.to_dict()) for s in snaps])

    def progress(self, run_id: str) -> dict[WorkUnitState, int]:
        counts = {state: 0 for state in WorkUnitState}
        for snap in self._units_col(run_id).stream():
            counts[WorkUnitState(snap.to_dict()["state"])] += 1
        return counts

    def close(self) -> None:
        pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _claim_in_transaction(transaction: Any, units_col: Any, run_id: str) -> WorkUnit | None:
    """Take the lowest-index claimable unit atomically.

    Wrapped by ``firestore.transactional`` so Firestore retries on contention:
    two workers reading the same unit force one transaction to abort and retry,
    which then sees the unit already ``running`` and moves to the next.
    """
    from google.cloud import firestore

    @firestore.transactional
    def _claim(txn: Any) -> WorkUnit | None:
        query = (
            units_col.where("state", "in", list(_CLAIMABLE))
            .order_by("unit_index")
            .limit(1)
        )
        docs = list(query.stream(transaction=txn))
        if not docs:
            return None
        snap = docs[0]
        data = snap.to_dict()
        txn.update(
            snap.reference,
            {
                "state": WorkUnitState.RUNNING.value,
                "attempts": data.get("attempts", 0) + 1,
                "updated_at": _now(),
            },
        )
        data["state"] = WorkUnitState.RUNNING.value
        data["attempts"] = data.get("attempts", 0) + 1
        return doc_to_unit(run_id, data)

    return _claim(transaction)
