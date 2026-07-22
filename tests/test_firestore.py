"""Firestore state-store tests.

Scoped honestly: the record↔document mapping and the claimable-run gating are
pure functions with no SDK dependency, and they are tested directly here. The
atomic claim itself is a Firestore transaction guarantee — a dict fake cannot
prove distributed-transaction correctness, so it is not asserted here. An
emulator-gated integration test (skipped unless ``FIRESTORE_EMULATOR_HOST`` is
set) covers the real claim.

The batch-limit tests sit in between: a fake client rejects any commit over 500
writes, the way Firestore itself does. That proves *our* chunking, not
Firestore's limit — the limit is documented, and asserting it against a fake
would prove nothing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import Run, WorkUnit
from decimal import Decimal as D

from docloom.core.state.firestore import (
    _BATCH_LIMIT,
    _chunks,
    _doc_id,
    doc_lease_is_expired,
    doc_to_spend,
    doc_to_run,
    doc_to_unit,
    run_is_claimable,
    run_to_doc,
    unit_to_doc,
)


def a_run(**kw: object) -> Run:
    base = dict(run_id="run_1", pack="invoice", config_id="cfg", total_units=3)
    base.update(kw)
    return Run(**base)  # type: ignore[arg-type]


def a_unit(**kw: object) -> WorkUnit:
    base = dict(run_id="run_1", unit_index=2, start_index=2000, count=1000)
    base.update(kw)
    return WorkUnit(**base)  # type: ignore[arg-type]


# ── Pure mapping round-trips ────────────────────────────────────────────────
def test_run_document_roundtrip() -> None:
    run = a_run(state=RunState.RUNNING, metadata={"k": "v"})
    restored = doc_to_run(run.run_id, run_to_doc(run))
    assert restored == run


def test_unit_document_roundtrip() -> None:
    unit = a_unit(state=WorkUnitState.FAILED, attempts=2, error="boom")
    restored = doc_to_unit(unit.run_id, unit_to_doc(unit))
    assert restored == unit


def test_unit_end_index_survives_mapping() -> None:
    unit = doc_to_unit("run_1", unit_to_doc(a_unit(start_index=5000, count=1000)))
    assert unit.end_index == 6000


def test_lease_survives_mapping() -> None:
    lease = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    unit = a_unit(state=WorkUnitState.RUNNING, lease_expires_at=lease)
    doc = unit_to_doc(unit)
    assert doc["lease_expires_at"] == "2026-07-20T12:00:00+00:00"
    assert doc_to_unit("run_1", doc).lease_expires_at == lease


def test_missing_lease_maps_to_none() -> None:
    assert doc_to_unit("run_1", unit_to_doc(a_unit())).lease_expires_at is None


# ── Expired-lease selection (the reclaim predicate, pure) ────────────────────
def test_expired_lease_only_for_running_with_a_lapsed_lease() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    past, future = now - timedelta(minutes=1), now + timedelta(minutes=1)

    running_lapsed = unit_to_doc(a_unit(state=WorkUnitState.RUNNING, lease_expires_at=past))
    running_live = unit_to_doc(a_unit(state=WorkUnitState.RUNNING, lease_expires_at=future))
    running_no_lease = unit_to_doc(a_unit(state=WorkUnitState.RUNNING))
    pending = unit_to_doc(a_unit(state=WorkUnitState.PENDING, lease_expires_at=past))

    assert doc_lease_is_expired(running_lapsed, now) is True
    assert doc_lease_is_expired(running_live, now) is False
    assert doc_lease_is_expired(running_no_lease, now) is False   # never leased
    assert doc_lease_is_expired(pending, now) is False            # not running


def test_timestamps_are_iso_strings_in_the_document() -> None:
    """Firestore stores them as strings so the store is portable and diffable."""
    doc = run_to_doc(a_run(created_at=datetime(2026, 7, 15, tzinfo=UTC)))
    assert doc["created_at"] == "2026-07-15T00:00:00+00:00"


# ── Claimable-run gating ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("state", "claimable"),
    [
        (RunState.PENDING, True),
        (RunState.RUNNING, True),
        (RunState.PAUSED, False),
        (RunState.CANCELLED, False),
        (RunState.COMPLETED, True),   # completed runs simply have no units left
    ],
)
def test_run_is_claimable_gates_on_state(state: RunState, claimable: bool) -> None:
    assert run_is_claimable(a_run(state=state)) is claimable


def test_run_is_claimable_false_for_missing_run() -> None:
    assert run_is_claimable(None) is False


# ── Real transaction — emulator only ────────────────────────────────────────
requires_emulator = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="needs the Firestore emulator (set FIRESTORE_EMULATOR_HOST)",
)


def _emu_store(lease_seconds: float = 900):  # noqa: ANN202 - emulator only
    from docloom.core.state.firestore import FirestoreStateStore

    return FirestoreStateStore(project="docloom-test", lease_seconds=lease_seconds)


def _emu_run(store, units: int, *, lease_seconds: float = 900) -> str:  # noqa: ANN001
    import uuid
    run_id = f"emu_{uuid.uuid4().hex[:10]}"
    store.create_run(a_run(run_id=run_id, total_units=units), [
        WorkUnit(run_id=run_id, unit_index=i, start_index=i * 1000, count=1000)
        for i in range(units)
    ])
    return run_id


@requires_emulator
def test_atomic_claim_against_emulator() -> None:  # pragma: no cover - emulator only
    store = _emu_store()
    run_id = _emu_run(store, 2)
    seen = []
    while (u := store.claim_next_unit(run_id)) is not None:
        seen.append(u.unit_index)
    assert sorted(seen) == [0, 1]
    assert len(seen) == len(set(seen))   # never claimed twice


@requires_emulator
def test_claim_stamps_lease_and_complete_clears_it_against_emulator() -> None:  # pragma: no cover
    store = _emu_store()
    run_id = _emu_run(store, 1)
    unit = store.claim_next_unit(run_id)
    assert unit is not None and unit.lease_expires_at is not None
    store.complete_unit(run_id, 0)
    done = next(u for u in store.units(run_id) if u.unit_index == 0)
    assert done.state is WorkUnitState.DONE
    assert done.lease_expires_at is None
    assert store.progress(run_id)[WorkUnitState.DONE] == 1


@requires_emulator
def test_reclaim_recovers_a_crashed_unit_against_emulator() -> None:  # pragma: no cover
    # lease_seconds=0 -> the claimed unit's lease is already expired.
    store = _emu_store(lease_seconds=0)
    run_id = _emu_run(store, 2)
    crashed = store.claim_next_unit(run_id)              # unit 0, never completed
    assert crashed is not None and crashed.unit_index == 0
    assert store.reclaim_expired_units(run_id) == 1      # abandoned unit recovered
    assert store.reclaim_expired_units(run_id) == 0      # idempotent
    again = store.claim_next_unit(run_id)
    assert again is not None and again.unit_index == 0
    assert again.attempts == 2                           # attempts preserved


@requires_emulator
def test_reclaim_leaves_live_leases_alone_against_emulator() -> None:  # pragma: no cover
    store = _emu_store(lease_seconds=900)
    run_id = _emu_run(store, 1)
    store.claim_next_unit(run_id)
    assert store.reclaim_expired_units(run_id) == 0      # lease still valid
    assert store.progress(run_id)[WorkUnitState.RUNNING] == 1


# ── Spend rollup ────────────────────────────────────────────────────────────
def test_total_row_gets_a_legal_document_id() -> None:
    """'*' is not a valid Firestore document id, and '/' would nest a path."""
    assert _doc_id("*") == "__total__"
    assert "/" not in _doc_id("vendor/model-1")


def test_spend_document_mapping_round_trips_the_cost() -> None:
    row = doc_to_spend("r", {"model": "m", "cost_nano": 3_000_000, "calls": 2})
    assert row.cost_usd == D("0.003")
    assert row.calls == 2


def test_spend_is_stored_as_integer_nano_not_a_double() -> None:
    """Firestore's Increment is int-or-double, and money must never accumulate in
    a double — so the counter is an integer nano-dollar count."""
    row = doc_to_spend("r", {"model": "m", "cost_nano": 400})
    assert row.cost_usd == D("0.0000004")


@requires_emulator
def test_add_spend_accumulates_atomically_against_the_emulator() -> None:  # pragma: no cover
    store = _emu_store()
    run_id = _emu_run(store, 1)
    for _ in range(30):
        store.add_spend(run_id, "claude-haiku-4-5", cost=D("0.0000004"))
    assert store.total_spend(run_id) == D("0.000012")     # 30 x 4e-7
    by_model = {r.model: r for r in store.spend(run_id)}
    assert by_model["claude-haiku-4-5"].calls == 30
    assert by_model["*"].cost_usd == D("0.000012")


# ── Planning: batch chunking and the claim gate ─────────────────────────────
def test_writes_are_chunked_to_firestores_batch_limit() -> None:
    """A WriteBatch is capped at 500 operations. Planning a run of 1,000 units in
    one batch is rejected outright, which broke exactly the production-scale runs
    this store exists for."""
    assert _BATCH_LIMIT == 500
    sizes = [len(c) for c in _chunks(list(range(1000)))]
    assert sizes == [500, 500]
    assert [len(c) for c in _chunks(list(range(1001)))] == [500, 500, 1]


def test_chunking_preserves_order_and_loses_nothing() -> None:
    items = list(range(1234))
    assert [i for chunk in _chunks(items) for i in chunk] == items


def test_chunking_a_small_plan_is_a_single_batch() -> None:
    assert _chunks(list(range(7))) == [list(range(7))]
    assert _chunks([]) == []


def test_a_half_written_plan_yields_no_claims() -> None:
    """The window Firestore cannot avoid: the run marker exists but its units are
    still being written. Claiming then would look like an empty run."""
    assert run_is_claimable(a_run(state=RunState.RUNNING, planned=False)) is False
    assert run_is_claimable(a_run(state=RunState.RUNNING, planned=True)) is True


def test_a_document_written_before_the_flag_reads_as_planned() -> None:
    """Absent means planned: the old path only made a run visible once its units
    were committed, so defaulting the other way would strand every existing run."""
    doc = run_to_doc(a_run())
    assert "planned" not in doc
    assert doc_to_run("run_1", doc).planned is True


@requires_emulator
def test_only_one_planner_wins_against_emulator() -> None:  # pragma: no cover - emulator only
    store = _emu_store()
    import uuid
    run_id = f"emu_{uuid.uuid4().hex[:10]}"
    run = a_run(run_id=run_id, total_units=2)
    units = [WorkUnit(run_id=run_id, unit_index=i, start_index=i * 1000, count=1000)
             for i in range(2)]
    assert store.create_run(run, units) is True
    assert store.create_run(run, units) is False


@requires_emulator
def test_a_late_planner_cannot_reset_a_claimed_unit_against_emulator() -> None:  # pragma: no cover
    store = _emu_store()
    run_id = _emu_run(store, 2)
    claimed = store.claim_next_unit(run_id)
    assert claimed is not None

    run = a_run(run_id=run_id, total_units=2)
    units = [WorkUnit(run_id=run_id, unit_index=i, start_index=i * 1000, count=1000)
             for i in range(2)]
    assert store.create_run(run, units) is False

    still = {u.unit_index: u for u in store.units(run_id)}[claimed.unit_index]
    assert still.state is WorkUnitState.RUNNING


@requires_emulator
def test_a_plan_larger_than_one_batch_lands_whole_against_emulator() -> None:  # pragma: no cover
    """The regression test for the batch limit: 600 units is over the 500 cap."""
    store = _emu_store()
    run_id = _emu_run(store, 600)
    assert len(list(store.units(run_id))) == 600
    assert store.get_run(run_id).planned is True   # type: ignore[union-attr]


# ── Planning at production scale (fake client, no SDK) ──────────────────────
class _FakeBatch:
    """A WriteBatch that rejects an oversized commit the way Firestore does:
    'Transaction or batch is too large. Maximum 500 writes allowed per request'."""

    def __init__(self, client: "_FakeClient") -> None:
        self._client, self._ops = client, []

    def set(self, ref: "_FakeDoc", doc: dict) -> None:
        self._ops.append((ref.path, doc))

    update = set

    def commit(self) -> None:
        if len(self._ops) > 500:
            raise ValueError(
                f"Transaction or batch is too large ({len(self._ops)} writes). "
                "Maximum 500 writes allowed per request."
            )
        self._client.commits.append(len(self._ops))
        self._client.docs.update(dict(self._ops))


class _FakeDoc:
    def __init__(self, path: str) -> None:
        self.path = path

    def collection(self, name: str) -> "_FakeCol":
        return _FakeCol(f"{self.path}/{name}")


class _FakeCol:
    def __init__(self, path: str) -> None:
        self.path = path

    def document(self, doc_id: str) -> _FakeDoc:
        return _FakeDoc(f"{self.path}/{doc_id}")


class _FakeClient:
    def __init__(self) -> None:
        self.commits: list[int] = []
        self.docs: dict[str, dict] = {}

    def collection(self, name: str) -> _FakeCol:
        return _FakeCol(name)

    def batch(self) -> _FakeBatch:
        return _FakeBatch(self)


def _fake_store():  # noqa: ANN202
    from docloom.core.state.firestore import FirestoreStateStore

    client = _FakeClient()
    return FirestoreStateStore(project="p", client=client), client


def test_planning_a_thousand_units_does_not_blow_the_batch_limit() -> None:
    """The regression: run and units went into one unchunked WriteBatch, so any
    run over 499 units failed to plan at all — exactly the production scale this
    store exists for. 1,000 units must land as three legal commits."""
    store, client = _fake_store()
    units = [WorkUnit(run_id="r", unit_index=i, start_index=i * 100, count=100)
             for i in range(1000)]

    store._write_units("r", units)                     # would raise before the fix

    assert client.commits == [500, 500]
    assert len(client.docs) == 1000
    assert client.docs["runs/r/work_units/999"]["start_index"] == 99_900


def test_every_unit_is_written_exactly_once_across_chunks() -> None:
    store, client = _fake_store()
    units = [WorkUnit(run_id="r", unit_index=i, start_index=i * 100, count=100)
             for i in range(1234)]
    store._write_units("r", units)
    assert sum(client.commits) == 1234
    assert len(client.docs) == 1234                    # no path written twice
    assert {d["unit_index"] for d in client.docs.values()} == set(range(1234))


def test_a_plan_under_the_limit_is_still_a_single_commit() -> None:
    """Chunking must not cost small runs an extra round trip."""
    store, client = _fake_store()
    store._write_units("r", [WorkUnit(run_id="r", unit_index=i, start_index=i, count=1)
                             for i in range(10)])
    assert client.commits == [10]
