"""Firestore state store (``firestore://project/database``).

The scale path for the atomic claim. Where SQLite serialises workers with a
local write lock, Firestore serialises them with a document transaction — so
the Cloud Run generation Service can run many concurrent instances against one
run and still never generate the same shard twice.

Layout: one document per run in ``runs``, with a ``work_units`` subcollection.

**On create_run.** A WriteBatch caps at 500 operations, so a run's plan cannot be
one atomic write — a 1,000-unit run needs three. The run marker is therefore
written first with a conditional ``create()``, which serialises planners (exactly
one of N simultaneously-starting workers wins), and carries ``planned: False``
until every unit has landed, so a half-written plan is distinguishable from a
finished run rather than reading as "nothing left to do".

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
    PLANNING_TAKEOVER_SECONDS,
    TOTAL_MODEL,
    Run,
    Spend,
    WorkUnit,
    from_nano,
    to_nano,
)

_CLAIMABLE = WorkUnitState.PENDING.value   # claim pending only; see StateStore

#: Firestore caps a WriteBatch at 500 operations, so every batched write here has
#: to chunk: a run of 1,000 units is 1,000 writes and a single batch would be
#: rejected outright. At the default ``unit_size`` of 1,000 that ceiling is a
#: 500k-document run — well inside what this store exists to serve.
_BATCH_LIMIT = 500


def _chunks(items: list[Any], size: int = _BATCH_LIMIT) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


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
        # Absent on documents written before the flag existed; those runs were
        # always fully planned before they became visible.
        planned=bool(doc.get("planned", True)),
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
    ``/`` would nest a path, so both are escaped.

    The total sentinel is ``_total_`` rather than the more obvious ``__total__``:
    Firestore rejects any id matching ``__.*__`` as reserved, so a double-
    underscore wrapper is a 400 (``InvalidArgument``) the instant a distributed
    budget writes its rollup. ``_total_`` has no ``__`` substring at all, and no
    real model name escapes to it (model ids never start with ``/``)."""
    return "_total_" if model == TOTAL_MODEL else model.replace("/", "_")


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
    """A run yields claims only while it exists, is fully planned, and is neither
    paused nor cancelled.

    The ``planned`` check matters as much as the state one: a run whose units are
    still being written has nothing to claim *yet*, and treating that as "nothing
    to claim ever" would send every worker home from a run that had not started.
    """
    return (
        run is not None
        and run.planned
        and run.state not in (RunState.PAUSED, RunState.CANCELLED)
    )


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

    def create_run(self, run: Run, units: list[WorkUnit]) -> bool:
        """Claim the run id with a conditional create, then write the units.

        `document.create()` fails if the document exists, which is what makes
        exactly one of N simultaneously-starting workers the planner. The marker
        goes down first with ``planned: False`` so the run is never briefly
        visible-but-empty — a state every worker would read as "already
        finished".
        """
        from google.api_core.exceptions import AlreadyExists

        marker = run_to_doc(run) | {"planned": False, "planning_started_at": _now()}
        try:
            self._run_ref(run.run_id).create(marker)
        except AlreadyExists:
            if not self._should_take_over(run.run_id):
                return False
            # The previous planner died mid-plan. Unit writes are keyed by index
            # and byte-identical, so finishing its work is safe.
        self._write_units(run.run_id, units)
        self._run_ref(run.run_id).update({"planned": True, "updated_at": _now()})
        return True

    def _should_take_over(self, run_id: str) -> bool:
        """True when an unplanned marker has sat long enough to be abandoned."""
        snap = self._run_ref(run_id).get()
        if not snap.exists:
            return True                      # vanished between create and read
        doc = snap.to_dict()
        if doc.get("planned", True):
            return False                     # someone finished the plan
        started = doc.get("planning_started_at")
        if not started:
            return True
        age = (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()
        return age >= PLANNING_TAKEOVER_SECONDS

    def _write_units(self, run_id: str, units: list[WorkUnit]) -> None:
        units_col = self._units_col(run_id)
        for chunk in _chunks(list(units)):
            batch = self._client.batch()
            for unit in chunk:
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
        refs = [snap.reference for snap in query.stream()]
        # Chunked: a run with more than 500 failed units would otherwise exceed
        # Firestore's batch limit and the whole resume would fail.
        for chunk in _chunks(refs):
            batch = self._client.batch()
            for ref in chunk:
                batch.update(
                    ref,
                    {"state": WorkUnitState.PENDING.value, "error": None,
                     "lease_expires_at": None, "updated_at": _now()},
                )
            batch.commit()
        return len(refs)

    def reclaim_expired_units(self, run_id: str, *, now: datetime | None = None) -> int:
        # Query only running units (an equality filter needs no composite index),
        # then filter expired leases in Python — the running set is small next to
        # the whole run, and this keeps the reclaim index-free.
        cutoff = now or datetime.now(UTC)
        query = _where_state(self._units_col(run_id), WorkUnitState.RUNNING.value)
        refs = [snap.reference for snap in query.stream()
                if doc_lease_is_expired(snap.to_dict(), cutoff)]
        for chunk in _chunks(refs):
            batch = self._client.batch()
            for ref in chunk:
                batch.update(
                    ref,
                    {"state": WorkUnitState.PENDING.value,
                     "lease_expires_at": None, "updated_at": _now()},
                )
            batch.commit()
        return len(refs)

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
