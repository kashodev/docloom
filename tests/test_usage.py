"""LLM usage telemetry tests.

Three things carry the weight:

* **cost stays exact** — a completion can cost a fraction of a cent, so a float
  anywhere in the path silently loses the long tail across a million calls;
* **a retry cannot double-count** — spend counted twice is the classic failure of
  bolt-on cost telemetry, and every backend keys rows so a replay overwrites;
* **it is on by default, and free when there is nothing to record** — a
  procedural pack makes no LLM calls, so it writes no rows and no shard.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from docloom.core.pipeline.golden import decode_shard
from docloom.core.providers import CatalogueItem, CatalogueRunner, CompletionRequest, ProviderMix
from docloom.core.providers.base import CompletionResult, Usage
from docloom.core.providers.pricing import pricing_for
from docloom.core.storage.local import LocalBlobStore
from docloom.core.usage import (
    DEFAULT_USAGE_URI,
    LlmUsage,
    MemoryUsageSink,
    NullUsageSink,
    ShardUsageSink,
    UsageSink,
    open_usage_sink,
)


def run(coro):
    return asyncio.run(coro)


def a_usage(**kw) -> LlmUsage:
    base = dict(run_id="r1", provider="anthropic", model="claude-haiku-4-5",
                input_tokens=500, output_tokens=90, cost_usd=D("0.00095"))
    base.update(kw)
    return LlmUsage(**base)  # type: ignore[arg-type]


def a_result(cost: D = D("0.00095")) -> CompletionResult:
    return CompletionResult("text", Usage(500, 90, 0), "claude-haiku-4-5", "anthropic", cost)


# ── The row ─────────────────────────────────────────────────────────────────
def test_cost_stays_an_exact_decimal() -> None:
    """A fraction of a cent must survive; float would round a million of these
    into nonsense."""
    row = a_usage(cost_usd=D("0.0000004")).to_row()
    assert isinstance(row["cost_usd"], D)
    assert row["cost_usd"] == D("0.0000004")


def test_totals_are_derived_not_stored_twice() -> None:
    assert a_usage(input_tokens=500, output_tokens=90).total_tokens == 590
    assert a_usage().to_row()["total_tokens"] == 590


def test_from_completion_reads_correlation_ids_from_request_metadata() -> None:
    usage = LlmUsage.from_completion(
        a_result(),
        metadata={"run_id": "r9", "unit_index": 3, "document_id": "inv_7",
                  "pack": "contract", "purpose": "clauses", "call_index": 2},
        latency_ms=120,
    )
    assert (usage.run_id, usage.unit_index, usage.document_id) == ("r9", 3, "inv_7")
    assert (usage.pack, usage.purpose, usage.call_index) == ("contract", "clauses", 2)
    assert usage.cost_usd == D("0.00095") and usage.latency_ms == 120
    assert usage.status == "ok"


def test_a_failed_call_is_recorded_not_dropped() -> None:
    """A failure is often still billed, and a cost table that omits failures
    understates what the run actually spent."""
    usage = LlmUsage.failure(provider="anthropic", model="m", error="boom",
                             metadata={"run_id": "r1"})
    assert usage.status == "error" and usage.error == "boom"
    assert usage.cost_usd == D(0)


# ── The sink protocol ───────────────────────────────────────────────────────
@pytest.mark.parametrize("sink", [NullUsageSink(), MemoryUsageSink()])
def test_sinks_satisfy_the_protocol(sink: object) -> None:
    assert isinstance(sink, UsageSink)


def test_null_sink_records_nothing() -> None:
    sink = NullUsageSink()
    sink.record(a_usage())
    assert sink.flush() == 0


# ── The sharded sink (the default) ──────────────────────────────────────────
def test_shard_sink_writes_beside_the_golden_data(tmp_path) -> None:
    blob = LocalBlobStore(tmp_path)
    sink = ShardUsageSink(blob, "r1")
    sink.record(a_usage(unit_index=7))
    assert sink.flush() == 1

    key = "r1/golden/llm_usage/unit-000007.jsonl.gz"
    assert blob.exists(key)
    rows = decode_shard(blob.get(key))
    assert rows[0]["model"] == "claude-haiku-4-5"
    # The golden codec restores Decimal exactly, which is why it is reused here.
    assert rows[0]["cost_usd"] == D("0.00095")


def test_a_retried_unit_overwrites_and_cannot_double_count(tmp_path) -> None:
    """The property that makes bolt-on telemetry go wrong. Same unit replayed =
    same key = overwritten, so spend is never counted twice."""
    blob = LocalBlobStore(tmp_path)
    key = "r1/golden/llm_usage/unit-000007.jsonl.gz"

    first = ShardUsageSink(blob, "r1")
    for _ in range(3):
        first.record(a_usage(unit_index=7))
    first.flush()
    assert len(decode_shard(blob.get(key))) == 3

    replay = ShardUsageSink(blob, "r1")          # the unit is retried
    for _ in range(3):
        replay.record(a_usage(unit_index=7))
    replay.flush()
    assert len(decode_shard(blob.get(key))) == 3, "replay appended instead of overwriting"


def test_shard_sink_writes_nothing_when_there_was_nothing_to_record(tmp_path) -> None:
    """A procedural run must not litter the bucket with empty shards."""
    blob = LocalBlobStore(tmp_path)
    sink = ShardUsageSink(blob, "r1")
    assert sink.flush() == 0
    assert list(blob.iter_keys("")) == []


def test_catalogue_rows_land_in_their_own_shard(tmp_path) -> None:
    """A catalogue build has no work units, so its rows cannot borrow a unit name."""
    blob = LocalBlobStore(tmp_path)
    sink = ShardUsageSink(blob, "r1")
    sink.record(a_usage(catalogue_item_id="clause-1"))
    sink.flush()
    assert blob.exists("r1/golden/llm_usage/catalogue-000000.jsonl.gz")


# ── The factory: on by default ──────────────────────────────────────────────
def test_the_default_is_on_not_off(tmp_path) -> None:
    """The whole point: you get cost data unless you say otherwise."""
    sink = open_usage_sink(None, blob=LocalBlobStore(tmp_path), run_id="r1")
    assert isinstance(sink, ShardUsageSink)
    assert not isinstance(sink, NullUsageSink)
    assert DEFAULT_USAGE_URI.startswith("shard")


@pytest.mark.parametrize("spelling", ["off", "none", "no", "disabled", "null", "null://", "OFF"])
def test_telemetry_can_be_turned_off_in_the_obvious_ways(spelling: str) -> None:
    assert isinstance(open_usage_sink(spelling), NullUsageSink)


def test_shard_scheme_needs_the_runs_blob_store() -> None:
    with pytest.raises(ValueError, match="blob store"):
        open_usage_sink("shard://")


def test_unknown_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported usage scheme"):
        open_usage_sink("carrier-pigeon://somewhere")


def test_cloud_schemes_dispatch_without_the_sdk_installed() -> None:
    """The adapters are lazy-imported, so the factory resolves them even though
    neither SDK is present in the default environment."""
    for uri, extra in (("firestore://p/(default)", r"docloom\[gcp\]"),
                       ("dynamodb://usage", r"docloom\[aws\]")):
        with pytest.raises(ImportError, match=extra):
            open_usage_sink(uri)


# ── Wiring: the mix and the catalogue runner ────────────────────────────────
class StubProvider:
    pricing = pricing_for("__local__")
    name, model = "stub", "stub-1"

    def __init__(self, cost: D = D("0.001"), fail: bool = False) -> None:
        self._cost, self._fail = cost, fail

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if self._fail:
            raise RuntimeError("model blip")
        return CompletionResult("t", Usage(10, 5, 0), self.model, self.name, self._cost)

    def estimate_cost(self, request: CompletionRequest) -> D:
        return self._cost


def test_the_mix_records_every_completion() -> None:
    sink = MemoryUsageSink()
    mix = ProviderMix([StubProvider()], [1.0], usage=sink)
    run(mix.complete(CompletionRequest(system="s", prompt="p",
                                       metadata={"run_id": "r1", "document_id": "d1"}), seed=1))
    assert len(sink.rows) == 1
    assert sink.rows[0].document_id == "d1"
    assert sink.rows[0].latency_ms is not None


def test_the_mix_records_failures_and_still_raises() -> None:
    sink = MemoryUsageSink()
    mix = ProviderMix([StubProvider(fail=True)], [1.0], usage=sink)
    with pytest.raises(RuntimeError):
        run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))
    assert sink.rows[0].status == "error"


def test_the_mix_is_unaffected_when_no_sink_is_given() -> None:
    mix = ProviderMix([StubProvider()], [1.0])
    result = run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))
    assert result.cost == D("0.001")


def test_the_catalogue_runner_attributes_cost_to_the_item_not_a_document() -> None:
    """A catalogue item is reused across many documents, so its cost is amortised
    later — recording it as a document cost would claim a precision we do not have."""
    sink = MemoryUsageSink()
    items = [CatalogueItem(f"clause-{i}", CompletionRequest(system="s", prompt=f"p{i}"))
             for i in range(4)]
    report = run(CatalogueRunner(ProviderMix([StubProvider()], [1.0]), usage=sink).run(items))

    assert len(sink.rows) == 4
    assert {r.catalogue_item_id for r in sink.rows} == {f"clause-{i}" for i in range(4)}
    assert all(r.document_id is None for r in sink.rows)
    # And the recorded spend agrees with the report's own total.
    assert sink.total_cost == report.total_cost


def test_the_runner_flushes_so_nothing_is_left_buffered() -> None:
    sink = MemoryUsageSink()
    items = [CatalogueItem("a", CompletionRequest(system="s", prompt="p"))]
    run(CatalogueRunner(ProviderMix([StubProvider()], [1.0]), usage=sink).run(items))
    assert sink.flushes >= 1


def test_batch_calls_are_flagged_because_they_are_half_price() -> None:
    class BatchStub(StubProvider):
        def estimate_batch_cost(self, request: CompletionRequest) -> D:
            return self._cost

        async def complete_batch(self, requests):
            return [CompletionResult("t", Usage(10, 5, 0), self.model, self.name, self._cost)
                    for _ in requests]

    sink = MemoryUsageSink()
    items = [CatalogueItem("a", CompletionRequest(system="s", prompt="p"))]
    run(CatalogueRunner(ProviderMix([BatchStub()], [1.0]), usage=sink).run(items))
    assert sink.rows[0].is_batch is True


def test_started_at_is_timezone_aware() -> None:
    assert a_usage().started_at.tzinfo is not None
    assert datetime.now(UTC) - a_usage().started_at < __import__("datetime").timedelta(minutes=1)
