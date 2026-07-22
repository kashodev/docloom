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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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

_CLAIMABLE = WorkUnitState.PENDING.value   # claim pending only; see StateStore


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
        "lease_expires_at": unit.lease_expires_at.isoformat() if unit.lease_expires_at else None,
    }


def doc_to_unit(run_id: str, doc: dict[str, Any]) -> WorkUnit:
    lease = doc.get("lease_expires_at")
    return WorkUnit(
        run_id=run_id,
        unit_index=doc["unit_index"],
        start_index=doc["start_index"],
        count=doc["count"],
        state=WorkUnitState(doc["state"]),
        attempts=doc.get("attempts", 0),
        error=doc.get("error"),
        updated_at=datetime.fromisoformat(doc["updated_at"]),
        lease_expires_at=datetime.fromisoformat(lease) if lease else None,
    )


def _doc_id(model: str) -> str:
    """Model name as a document id. ``*`` (the run total) is not a legal id, and
    ``/`` would nest a path, so both are escaped."""
    return "__total__" if model == TOTAL_MODEL else model.replace("/", "_")


def doc_to_spend(run_id: str, doc: dict[str, Any]) -> Spend:
    """Firestore document → rollup row. Pure, so the mapping is unit-tested."""
    return Spend(
        run_id=run_id,
        model=doc.get("model", TOTAL_MODEL),
        cost_usd=from_nano(int(doc.get("cost_nano", 0))),
        calls=int(doc.get("calls", 0)),
        input_tokens=int(doc.get("input_tokens", 0)),
        output_tokens=int(doc.get("output_tokens", 0)),
    )


def run_is_claimable(run: Run | None) -> bool:
    """A run yields claims only while it is neither paused nor cancelled."""
    return run is not None and run.state not in (RunState.PAUSED, RunState.CANCELLED)


def _where_state(col: Any, value: str) -> Any:
    """``col`` filtered to a single ``state`` value, via the keyword ``filter=``
    form (positional ``where`` args are deprecated in the Firestore SDK)."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    return col.where(filter=FieldFilter("state", "==", value))


def doc_lease_is_expired(doc: dict[str, Any], now: datetime) -> bool:
    """True for a ``running`` unit document whose lease has lapsed as of ``now``.

    Pure, so the reclaim selection is unit-tested without the Firestore SDK.
    """
    if doc.get("state") != WorkUnitState.RUNNING.value:
        return False
    lease = doc.get("lease_expires_at")
    return lease is not None and datetime.fromisoformat(lease) <= now


# ── Adapter ─────────────────────────────────────────────────────────────────
class FirestoreStateStore:
    """A :class:`~docloom.core.state.base.StateStore` backed by Firestore."""

    scheme = "firestore"

    def __init__(
        self,
        project: str,
        database: str = "(default)",
        *,
        client: Any | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if client is None:
            try:
                from google.cloud import firestore
            except ImportError as exc:
                raise ImportError(
                    "firestore:// state needs the GCP extra — pip install 'docloom[gcp]'"
                ) from exc
            client = firestore.Client(project=project, database=database)
        self._client = client
        self._lease_seconds = lease_seconds

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
        transaction = self._client.transaction()
        return _claim_in_transaction(
            transaction, self._units_col(run_id), run_id, self._lease_seconds
        )

    def complete_unit(self, run_id: str, unit_index: int) -> None:
        # Clear the lease: a terminal unit is no longer held by any worker.
        self._units_col(run_id).document(str(unit_index)).update(
            {"state": WorkUnitState.DONE.value, "error": None,
             "lease_expires_at": None, "updated_at": _now()}
        )

    def fail_unit(self, run_id: str, unit_index: int, error: str) -> None:
        self._units_col(run_id).document(str(unit_index)).update(
            {"state": WorkUnitState.FAILED.value, "error": error,
             "lease_expires_at": None, "updated_at": _now()}
        )

    def reset_failed_units(self, run_id: str) -> int:
        query = _where_state(self._units_col(run_id), WorkUnitState.FAILED.value)
        batch = self._client.batch()
        count = 0
        for snap in query.stream():
            batch.update(
                snap.reference,
                {"state": WorkUnitState.PENDING.value, "error": None,
                 "lease_expires_at": None, "updated_at": _now()},
            )
            count += 1
        if count:
            batch.commit()
        return count

    def reclaim_expired_units(self, run_id: str, *, now: datetime | None = None) -> int:
        # Query only running units (an equality filter needs no composite index),
        # then filter expired leases in Python — the running set is small next to
        # the whole run, and this keeps the reclaim index-free.
        cutoff = now or datetime.now(UTC)
        query = _where_state(self._units_col(run_id), WorkUnitState.RUNNING.value)
        batch = self._client.batch()
        count = 0
        for snap in query.stream():
            if doc_lease_is_expired(snap.to_dict(), cutoff):
                batch.update(
                    snap.reference,
                    {"state": WorkUnitState.PENDING.value,
                     "lease_expires_at": None, "updated_at": _now()},
                )
                count += 1
        if count:
            batch.commit()
        return count

    # ── Spend rollup ────────────────────────────────────────────────────────
    def _spend_col(self, run_id: str) -> Any:
        return self._run_ref(run_id).collection("spend")

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
        """Increment the model row and the run total via ``firestore.Increment``.

        Increment is a server-side atomic operation, so concurrent workers do not
        need a transaction and cannot lose an update. It is **integer** increment
        here: Firestore's Increment is int-or-double, and accumulating money in a
        double is exactly what this project avoids everywhere else — hence
        nano-dollars as an int rather than USD as a float.
        """
        from google.cloud import firestore

        nano = to_nano(cost)
        col = self._spend_col(run_id)
        delta = {
            "cost_nano": firestore.Increment(nano),
            "calls": firestore.Increment(calls),
            "input_tokens": firestore.Increment(input_tokens),
            "output_tokens": firestore.Increment(output_tokens),
            "updated_at": _now(),
        }
        batch = self._client.batch()
        batch.set(col.document(_doc_id(model)), {**delta, "model": model}, merge=True)
        batch.set(col.document(_doc_id(TOTAL_MODEL)), {**delta, "model": TOTAL_MODEL},
                  merge=True)
        batch.commit()
        return self.total_spend(run_id)

    def spend(self, run_id: str) -> list[Spend]:
        rows = [doc_to_spend(run_id, s.to_dict()) for s in self._spend_col(run_id).stream()]
        return sorted(rows, key=lambda s: s.model)

    def total_spend(self, run_id: str) -> Decimal:
        snap = self._spend_col(run_id).document(_doc_id(TOTAL_MODEL)).get()
        return from_nano(int(snap.to_dict().get("cost_nano", 0))) if snap.exists else Decimal(0)

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


def _claim_in_transaction(
    transaction: Any, units_col: Any, run_id: str, lease_seconds: float
) -> WorkUnit | None:
    """Take the lowest-index claimable unit atomically, stamping a lease.

    Wrapped by ``firestore.transactional`` so Firestore retries on contention:
    two workers reading the same unit force one transaction to abort and retry,
    which then sees the unit already ``running`` and moves to the next.

    Unlike SQLite, the claim does not scan for expired leases (a per-claim scan
    of the running set is costly at Firestore scale); reclaim is the explicit
    :meth:`FirestoreStateStore.reclaim_expired_units`, called on resume and safe
    to run periodically.
    """
    from google.cloud import firestore

    @firestore.transactional
    def _claim(txn: Any) -> WorkUnit | None:
        query = (
            _where_state(units_col, _CLAIMABLE)
            .order_by("unit_index")
            .limit(1)
        )
        docs = list(query.stream(transaction=txn))
        if not docs:
            return None
        snap = docs[0]
        data = snap.to_dict()
        lease = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        txn.update(
            snap.reference,
            {
                "state": WorkUnitState.RUNNING.value,
                "attempts": data.get("attempts", 0) + 1,
                "lease_expires_at": lease,
                "updated_at": _now(),
            },
        )
        data["state"] = WorkUnitState.RUNNING.value
        data["attempts"] = data.get("attempts", 0) + 1
        data["lease_expires_at"] = lease
        return doc_to_unit(run_id, data)

    return _claim(transaction)
