"""Firestore and DynamoDB usage-sink tests.

Scoped like the state-store tests: the item/document mapping and the
deterministic key construction are pure and tested directly with no SDK. The
behaviour that actually matters — that a replayed unit **overwrites** rather than
appending, so spend is never counted twice — is a property of the store, so it is
exercised against a real one where available (moto / DynamoDB Local, Firestore
emulator) and skipped otherwise.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal as D

import pytest

from docsynth.core.usage.base import LlmUsage
from docsynth.core.usage.dynamodb import usage_sort_key, usage_to_item
from docsynth.core.usage.firestore import usage_doc_id, usage_to_doc


def a_usage(**kw) -> LlmUsage:
    base = dict(run_id="r1", provider="anthropic", model="claude-haiku-4-5",
                input_tokens=500, output_tokens=90, cost_usd=D("0.00095"))
    base.update(kw)
    return LlmUsage(**base)  # type: ignore[arg-type]


# ── DynamoDB mapping ────────────────────────────────────────────────────────
def test_dynamodb_item_uses_the_single_table_shape() -> None:
    """Same pk/sk shape as the state store, so one table can hold both."""
    item = usage_to_item(a_usage(unit_index=7), 3)
    assert item["pk"] == "r1"
    assert item["sk"].startswith("USAGE#")


def test_dynamodb_sort_keys_order_numerically_as_strings() -> None:
    keys = [usage_sort_key(u, s) for u, s in
            ((0, 0), (0, 9), (0, 10), (9, 0), (10, 0), (100, 5))]
    assert keys == sorted(keys)


def test_dynamodb_sort_key_is_deterministic_so_a_replay_overwrites() -> None:
    assert usage_sort_key(7, 3) == usage_sort_key(7, 3)


def test_a_catalogue_row_does_not_collide_with_unit_zero() -> None:
    assert usage_sort_key(None, 0) != usage_sort_key(0, 0)
    assert "cat" in usage_sort_key(None, 0)


def test_dynamodb_keeps_cost_exact() -> None:
    """DynamoDB's number type is arbitrary-precision decimal, and boto3 rejects
    float — so the sub-cent tail survives here."""
    item = usage_to_item(a_usage(cost_usd=D("0.0000004")), 0)
    assert isinstance(item["cost_usd"], D)
    assert item["cost_usd"] == D("0.0000004")


def test_dynamodb_item_drops_nulls() -> None:
    """Absent reads back cleanly and costs nothing to store."""
    item = usage_to_item(a_usage(), 0)
    assert "document_id" not in item
    assert item["model"] == "claude-haiku-4-5"


# ── Firestore mapping ───────────────────────────────────────────────────────
def test_firestore_doc_id_is_deterministic_so_a_replay_overwrites() -> None:
    usage = a_usage(unit_index=7)
    assert usage_doc_id(usage, 3) == usage_doc_id(usage, 3)
    assert usage_doc_id(usage, 3) != usage_doc_id(usage, 4)


def test_firestore_keeps_cost_exact_as_a_string() -> None:
    """Firestore has no Decimal type and float would lose the sub-cent tail, so
    the exact value is a string; the float is for Firestore-side aggregation and
    is labelled approximate."""
    doc = usage_to_doc(a_usage(cost_usd=D("0.0000004")))
    assert doc["cost_usd"] == "0.0000004"
    assert D(doc["cost_usd"]) == D("0.0000004")
    assert isinstance(doc["cost_usd_approx"], float)


def test_firestore_catalogue_row_is_distinguishable_from_unit_zero() -> None:
    assert usage_doc_id(a_usage(), 0) != usage_doc_id(a_usage(unit_index=0), 0)


# ── DynamoDB, against a real store ──────────────────────────────────────────
_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT")


def _dynamo_backend() -> str | None:
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
    _dynamo_backend() is None,
    reason="needs boto3 + either DYNAMODB_ENDPOINT (DynamoDB Local) or moto",
)


@pytest.fixture
def dynamo_sink():
    import boto3

    from docsynth.core.state.dynamodb import DynamoDbStateStore
    from docsynth.core.usage.dynamodb import DynamoDbUsageSink

    mock = None
    if _dynamo_backend() == "moto":
        from moto import mock_aws

        mock = mock_aws()
        mock.start()
    try:
        resource = boto3.resource(
            "dynamodb", endpoint_url=_ENDPOINT, region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
        name = f"usage-{uuid.uuid4().hex[:12]}"
        # Deliberately the state store's table shape: one table serves both.
        table = DynamoDbStateStore.create_table(resource, name)
        table.wait_until_exists()
        yield DynamoDbUsageSink(name, resource=resource), table
    finally:
        if mock is not None:
            mock.stop()


@requires_dynamodb
def test_dynamodb_writes_rows_and_survives_the_batch_limit(dynamo_sink) -> None:
    """batch_writer chunks past DynamoDB's 25-item limit; prove it with 60."""
    sink, table = dynamo_sink
    for i in range(60):
        sink.record(a_usage(unit_index=1, call_index=i))
    assert sink.flush() == 60
    assert table.scan()["Count"] == 60


@requires_dynamodb
def test_dynamodb_replay_overwrites_rather_than_double_counting(dynamo_sink) -> None:
    sink, table = dynamo_sink
    for i in range(5):
        sink.record(a_usage(unit_index=2, call_index=i))
    sink.flush()
    assert table.scan()["Count"] == 5

    from docsynth.core.usage.dynamodb import DynamoDbUsageSink
    replay = DynamoDbUsageSink(table_resource=table)
    for i in range(5):
        replay.record(a_usage(unit_index=2, call_index=i))
    replay.flush()
    assert table.scan()["Count"] == 5, "replay appended instead of overwriting"


@requires_dynamodb
def test_dynamodb_round_trips_the_exact_cost(dynamo_sink) -> None:
    sink, table = dynamo_sink
    sink.record(a_usage(unit_index=1, cost_usd=D("0.0000004")))
    sink.flush()
    item = table.scan()["Items"][0]
    assert item["cost_usd"] == D("0.0000004")


@requires_dynamodb
def test_dynamodb_flush_is_empty_when_nothing_was_recorded(dynamo_sink) -> None:
    sink, table = dynamo_sink
    assert sink.flush() == 0
    assert table.scan()["Count"] == 0


# ── Firestore, against the emulator ─────────────────────────────────────────
requires_firestore = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="needs the Firestore emulator (set FIRESTORE_EMULATOR_HOST)",
)


@requires_firestore
def test_firestore_replay_overwrites_rather_than_double_counting() -> None:  # pragma: no cover
    from docsynth.core.usage.firestore import FirestoreUsageSink

    collection = f"usage_{uuid.uuid4().hex[:10]}"

    def write() -> None:
        sink = FirestoreUsageSink(project="docsynth-test", collection=collection)
        for i in range(5):
            sink.record(a_usage(unit_index=2, call_index=i))
        sink.flush()

    write()
    write()   # the unit is retried

    from google.cloud import firestore
    client = firestore.Client(project="docsynth-test")
    assert len(list(client.collection(collection).stream())) == 5
