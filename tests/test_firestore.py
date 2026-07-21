"""Firestore state-store tests.

Scoped honestly: the record↔document mapping and the claimable-run gating are
pure functions with no SDK dependency, and they are tested directly here. The
atomic claim itself is a Firestore transaction guarantee — a dict fake cannot
prove distributed-transaction correctness, so it is not asserted here. An
emulator-gated integration test (skipped unless ``FIRESTORE_EMULATOR_HOST`` is
set) covers the real claim.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import Run, WorkUnit
from docloom.core.state.firestore import (
    doc_lease_is_expired,
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
@pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="needs the Firestore emulator (set FIRESTORE_EMULATOR_HOST)",
)
def test_atomic_claim_against_emulator() -> None:  # pragma: no cover - emulator only
    from docloom.core.state.firestore import FirestoreStateStore

    store = FirestoreStateStore(project="docloom-test")
    run = a_run(run_id="emu_run", total_units=2)
    store.create_run(run, [
        WorkUnit(run_id="emu_run", unit_index=i, start_index=i * 1000, count=1000)
        for i in range(2)
    ])
    seen = []
    while (u := store.claim_next_unit("emu_run")) is not None:
        seen.append(u.unit_index)
    assert sorted(seen) == [0, 1]
    assert len(seen) == len(set(seen))   # never claimed twice
