"""DynamoDB state-store tests.

Scoped like the Firestore ones: the item mapping and sort-key ordering are pure
and tested directly with no SDK. The part that actually matters — the atomic
claim via a conditional write — is not something a hand-rolled fake can prove, so
the integration tests run against a real DynamoDB implementation, whichever is
available:

    pip install 'moto[dynamodb]'          # in-process, no Docker — the default
    pytest tests/test_dynamodb.py

    # or, higher fidelity, against the real engine:
    docker run -d -p 8000:8000 amazon/dynamodb-local
    DYNAMODB_ENDPOINT=http://localhost:8000 pytest tests/test_dynamodb.py

Both honour ``ConditionExpression`` (and raise ``ConditionalCheckFailedException``
when a race is lost), which is the property under test. Skipped when neither is
installed — the default environment has no boto3, matching the ``docloom[aws]``
extra being optional.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from docloom.core.enums import RunState, WorkUnitState
from docloom.core.state.base import Run, WorkUnit
from docloom.core.state.dynamodb import (
    item_to_run,
    item_to_spend,
    item_to_unit,
    run_to_item,
    spend_sort_key,
    unit_sort_key,
    unit_to_item,
)


def a_run(**kw: object) -> Run:
    base = dict(run_id="run_1", pack="invoice", config_id="cfg", total_units=3)
    base.update(kw)
    return Run(**base)  # type: ignore[arg-type]


def a_unit(**kw: object) -> WorkUnit:
    base = dict(run_id="run_1", unit_index=2, start_index=2000, count=1000)
    base.update(kw)
    return WorkUnit(**base)  # type: ignore[arg-type]


# ── Pure mapping ────────────────────────────────────────────────────────────
def test_run_item_roundtrip() -> None:
    run = a_run(state=RunState.RUNNING, metadata={"k": "v"})
    assert item_to_run(run_to_item(run)) == run


def test_unit_item_roundtrip() -> None:
    unit = a_unit(state=WorkUnitState.FAILED, attempts=2, error="boom")
    assert item_to_unit(unit_to_item(unit)) == unit


def test_lease_survives_mapping() -> None:
    lease = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    unit = a_unit(state=WorkUnitState.RUNNING, lease_expires_at=lease)
    assert item_to_unit(unit_to_item(unit)).lease_expires_at == lease
    assert item_to_unit(unit_to_item(a_unit())).lease_expires_at is None


def test_sort_keys_order_numerically_when_compared_as_strings() -> None:
    """The whole point of zero-padding: an ascending Query returns unit order."""
    keys = [unit_sort_key(i) for i in (0, 2, 9, 10, 99, 100, 1000, 12345678)]
    assert keys == sorted(keys)
    assert unit_sort_key(9) < unit_sort_key(10) < unit_sort_key(100)


def test_run_and_unit_items_share_the_partition_but_not_the_sort_key() -> None:
    run_item, unit_item = run_to_item(a_run()), unit_to_item(a_unit())
    assert run_item["pk"] == unit_item["pk"] == "run_1"
    assert run_item["sk"] == "RUN"
    assert unit_item["sk"].startswith("UNIT#")


# ── Real conditional claim — DynamoDB Local or moto ─────────────────────────
_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT")


def _backend() -> str | None:
    """Which real DynamoDB implementation we can test against, if any."""
    try:
        import boto3  # noqa: F401
    except ImportError:
        return None
    if _ENDPOINT:
        return "local"
    try:
        import moto  # noqa: F401
    except ImportError:
        return None
    return "moto"


requires_dynamodb = pytest.mark.skipif(
    _backend() is None,
    reason="needs boto3 + either DYNAMODB_ENDPOINT (DynamoDB Local) or moto",
)


@pytest.fixture
def store():
    import boto3

    from docloom.core.state.dynamodb import DynamoDbStateStore

    mock = None
    if _backend() == "moto":
        from moto import mock_aws

        mock = mock_aws()
        mock.start()
    try:
        resource = boto3.resource(
            "dynamodb", endpoint_url=_ENDPOINT, region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
        name = f"docloom-{uuid.uuid4().hex[:12]}"
        table = DynamoDbStateStore.create_table(resource, name)
        table.wait_until_exists()
        yield DynamoDbStateStore(name, resource=resource)
    finally:
        if mock is not None:
            mock.stop()


def seed(store, units: int = 4, run_id: str = "run_1", **kw):
    store.create_run(
        Run(run_id=run_id, pack="invoice", config_id="cfg", total_units=units,
            state=RunState.RUNNING),
        [WorkUnit(run_id=run_id, unit_index=i, start_index=i * 1000, count=1000)
         for i in range(units)],
    )
    return run_id


@requires_dynamodb
def test_create_and_read_back_a_run(store) -> None:
    seed(store, units=3)
    run = store.get_run("run_1")
    assert run is not None and run.pack == "invoice" and run.total_units == 3
    assert [u.unit_index for u in store.units("run_1")] == [0, 1, 2]


@requires_dynamodb
def test_claim_walks_units_in_order_then_returns_none(store) -> None:
    seed(store, units=3)
    claimed = [store.claim_next_unit("run_1") for _ in range(4)]
    assert [u.unit_index for u in claimed[:3]] == [0, 1, 2]  # type: ignore[union-attr]
    assert claimed[3] is None
    assert all(u.state is WorkUnitState.RUNNING for u in claimed[:3])  # type: ignore[union-attr]


@requires_dynamodb
def test_a_unit_is_never_claimed_twice(store) -> None:
    """The conditional write is the whole concurrency guarantee."""
    seed(store, units=6)
    seen = []
    while (u := store.claim_next_unit("run_1")) is not None:
        seen.append(u.unit_index)
    assert sorted(seen) == [0, 1, 2, 3, 4, 5]
    assert len(seen) == len(set(seen))


@requires_dynamodb
def test_a_lost_race_advances_to_the_next_unit(store) -> None:
    """Simulate the race directly: steal unit 0 out from under the claim by
    marking it running first; the claim must return unit 1, not fail."""
    seed(store, units=2)
    assert store._try_claim("run_1", 0, "2099-01-01T00:00:00+00:00") is not None
    # Unit 0 is no longer pending, so the conditional update on it fails.
    assert store._try_claim("run_1", 0, "2099-01-01T00:00:00+00:00") is None
    nxt = store.claim_next_unit("run_1")
    assert nxt is not None and nxt.unit_index == 1


@requires_dynamodb
def test_complete_and_fail_update_progress_and_clear_the_lease(store) -> None:
    seed(store, units=3)
    for _ in range(3):
        store.claim_next_unit("run_1")
    store.complete_unit("run_1", 0)
    store.fail_unit("run_1", 1, "boom")
    progress = store.progress("run_1")
    assert progress[WorkUnitState.DONE] == 1
    assert progress[WorkUnitState.FAILED] == 1
    assert progress[WorkUnitState.RUNNING] == 1
    by_index = {u.unit_index: u for u in store.units("run_1")}
    assert by_index[0].lease_expires_at is None
    assert by_index[1].lease_expires_at is None
    assert by_index[1].error == "boom"


@requires_dynamodb
def test_failed_units_rejoin_only_on_reset(store) -> None:
    seed(store, units=2)
    store.claim_next_unit("run_1")
    store.fail_unit("run_1", 0, "blip")
    nxt = store.claim_next_unit("run_1")
    assert nxt is not None and nxt.unit_index == 1
    store.complete_unit("run_1", 1)
    assert store.claim_next_unit("run_1") is None       # failed stays out
    assert store.reset_failed_units("run_1") == 1
    again = store.claim_next_unit("run_1")
    assert again is not None and again.unit_index == 0
    assert again.attempts == 2


@requires_dynamodb
def test_reclaim_recovers_a_crashed_unit(store) -> None:
    seed(store, units=2)
    crashed = store.claim_next_unit("run_1")             # never completed
    assert crashed is not None
    assert store.reclaim_expired_units("run_1") == 0      # lease still valid
    future = datetime.now(UTC) + timedelta(hours=1)
    assert store.reclaim_expired_units("run_1", now=future) == 1
    assert store.reclaim_expired_units("run_1", now=future) == 0   # idempotent
    again = store.claim_next_unit("run_1")
    assert again is not None and again.unit_index == 0 and again.attempts == 2


@requires_dynamodb
def test_paused_and_cancelled_runs_yield_no_claims(store) -> None:
    seed(store, units=2)
    store.set_run_state("run_1", RunState.PAUSED)
    assert store.claim_next_unit("run_1") is None
    store.set_run_state("run_1", RunState.CANCELLED)
    assert store.claim_next_unit("run_1") is None
    store.set_run_state("run_1", RunState.RUNNING)
    assert store.claim_next_unit("run_1") is not None


@requires_dynamodb
def test_paging_past_a_hundred_units(store) -> None:
    """create_run batches, and the unit query pages — prove both past one batch."""
    seed(store, units=120)
    assert len(list(store.units("run_1"))) == 120
    assert store.progress("run_1")[WorkUnitState.PENDING] == 120


# ── Spend rollup ────────────────────────────────────────────────────────────
def test_spend_sort_key_is_distinct_from_the_unit_range() -> None:
    """Rollup rows must not appear in the claim's begins_with('UNIT#') query."""
    assert not spend_sort_key("*").startswith("UNIT#")
    assert spend_sort_key("m") != unit_sort_key(0)


def test_spend_item_mapping_round_trips_the_cost() -> None:
    row = item_to_spend({"pk": "r", "model": "m", "cost_nano": 3_000_000, "calls": 2})
    assert row.cost_usd == D("0.003")
    assert row.calls == 2 and row.model == "m"


@requires_dynamodb
def test_add_spend_is_atomic_and_exact(store) -> None:
    """ADD is a server-side increment on DynamoDB's decimal number type, so the
    counter neither races nor loses precision."""
    seed(store, units=1)
    for _ in range(50):
        store.add_spend("run_1", "claude-haiku-4-5", cost=D("0.0000004"))
    assert store.total_spend("run_1") == D("0.00002")     # 50 x 4e-7

    by_model = {r.model: r for r in store.spend("run_1")}
    assert by_model["claude-haiku-4-5"].calls == 50
    assert by_model["*"].cost_usd == D("0.00002")


@requires_dynamodb
def test_add_spend_returns_the_running_total(store) -> None:
    seed(store, units=1)
    assert store.add_spend("run_1", "m", cost=D("0.01")) == D("0.01")
    assert store.add_spend("run_1", "m", cost=D("0.02")) == D("0.03")


@requires_dynamodb
def test_rollup_rows_do_not_disturb_the_unit_claim(store) -> None:
    """Both live in one partition, so prove the claim still walks units only."""
    seed(store, units=3)
    store.add_spend("run_1", "m", cost=D("0.01"))
    claimed = [store.claim_next_unit("run_1") for _ in range(4)]
    assert [u.unit_index for u in claimed[:3]] == [0, 1, 2]  # type: ignore[union-attr]
    assert claimed[3] is None
    assert len(list(store.units("run_1"))) == 3


# ── Planning: exactly one planner ───────────────────────────────────────────
def test_planned_survives_mapping() -> None:
    assert item_to_run(run_to_item(a_run(planned=False))).planned is False
    assert item_to_run(run_to_item(a_run())).planned is True


def test_an_item_written_before_the_flag_reads_as_planned() -> None:
    """Absent means planned: the old path only made a run visible once its units
    were written, so defaulting the other way would strand every existing run."""
    item = run_to_item(a_run())
    del item["planned"]
    assert item_to_run(item).planned is True


@requires_dynamodb
def test_the_first_planner_wins_and_the_second_is_told_it_lost(store) -> None:
    """Every worker in an array job starts by trying to plan. The conditional put
    is what makes that safe — the same primitive the unit claim uses."""
    run = Run(run_id="run_1", pack="invoice", config_id="cfg", total_units=2,
              state=RunState.RUNNING)
    units = [WorkUnit(run_id="run_1", unit_index=i, start_index=i * 1000, count=1000)
             for i in range(2)]
    assert store.create_run(run, units) is True
    assert store.create_run(run, units) is False


@requires_dynamodb
def test_a_late_planner_cannot_reset_a_claimed_unit(store) -> None:
    """The damage the race caused: worker B replans, unit 0 returns to pending,
    and two workers generate the same documents."""
    seed(store, units=3)
    claimed = store.claim_next_unit("run_1")
    assert claimed is not None and claimed.unit_index == 0

    run = Run(run_id="run_1", pack="invoice", config_id="cfg", total_units=3,
              state=RunState.RUNNING)
    units = [WorkUnit(run_id="run_1", unit_index=i, start_index=i * 1000, count=1000)
             for i in range(3)]
    assert store.create_run(run, units) is False

    still = {u.unit_index: u for u in store.units("run_1")}[0]
    assert still.state is WorkUnitState.RUNNING
    assert still.attempts == 1


@requires_dynamodb
def test_a_finished_plan_is_marked_planned(store) -> None:
    seed(store, units=2)
    run = store.get_run("run_1")
    assert run is not None and run.planned is True


@requires_dynamodb
def test_a_half_written_plan_yields_no_claims(store) -> None:
    """The window between the marker and the last unit: the run exists but has
    nothing to claim *yet*. A worker that claimed then would report a finished
    run that generated nothing."""
    seed(store, units=2)
    store._table.update_item(
        Key={"pk": "run_1", "sk": "RUN"},
        UpdateExpression="SET planned = :f",
        ExpressionAttributeValues={":f": False},
    )
    assert store.claim_next_unit("run_1") is None

    store._table.update_item(
        Key={"pk": "run_1", "sk": "RUN"},
        UpdateExpression="SET planned = :t",
        ExpressionAttributeValues={":t": True},
    )
    assert store.claim_next_unit("run_1") is not None


@requires_dynamodb
def test_an_abandoned_marker_is_taken_over(store) -> None:
    """A planner that dies mid-plan must not wedge the run forever: once its
    marker is older than the takeover window, the next worker replans it."""
    from docloom.core.state.base import PLANNING_TAKEOVER_SECONDS

    run = Run(run_id="run_1", pack="invoice", config_id="cfg", total_units=2,
              state=RunState.RUNNING)
    units = [WorkUnit(run_id="run_1", unit_index=i, start_index=i * 1000, count=1000)
             for i in range(2)]
    # A marker with no units behind it — the planner died right after writing it.
    stale = datetime.now(UTC) - timedelta(seconds=PLANNING_TAKEOVER_SECONDS + 60)
    store._table.put_item(Item=run_to_item(run) | {
        "planned": False, "planning_started_at": stale.isoformat(),
    })
    assert store.create_run(run, units) is True          # taken over
    assert len(list(store.units("run_1"))) == 2
    assert store.get_run("run_1").planned is True        # type: ignore[union-attr]


@requires_dynamodb
def test_a_live_marker_is_left_alone(store) -> None:
    """The mirror of the takeover: a planner still working must not be trampled."""
    run = Run(run_id="run_1", pack="invoice", config_id="cfg", total_units=2,
              state=RunState.RUNNING)
    units = [WorkUnit(run_id="run_1", unit_index=i, start_index=i * 1000, count=1000)
             for i in range(2)]
    store._table.put_item(Item=run_to_item(run) | {
        "planned": False, "planning_started_at": datetime.now(UTC).isoformat(),
    })
    assert store.create_run(run, units) is False
