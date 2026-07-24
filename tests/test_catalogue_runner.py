"""Catalogue-runner tests.

The runner drives a provider mix over many items under a budget, batching the
slice whose provider supports it. Tested with in-memory fake providers (no
network, no keys): deterministic routing, per-item results and cost accounting,
the batch path for a batch-capable provider, budget enforcement, and isolated
failures. The Anthropic batch request/parse mapping is tested against a fake
batches client.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

from docloom.core.providers import (
    BudgetGuard,
    CatalogueItem,
    CatalogueRunner,
    CompletionRequest,
    ProviderMix,
    item_seed,
    pricing_for,
)
from docloom.core.providers.anthropic_provider import (
    AnthropicProvider,
    batch_custom_id_index,
    build_batch_requests,
)
from docloom.core.providers.base import CompletionResult, Usage


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def items(n: int, *, start: int = 0) -> list[CatalogueItem]:
    return [CatalogueItem(f"item-{i}", CompletionRequest(system="s", prompt=f"p{i}"))
            for i in range(start, start + n)]


# ── Fake providers ──────────────────────────────────────────────────────────
class SyncStub:
    """A per-call provider that echoes its name and charges a fixed cost."""

    pricing = pricing_for("__local__")

    def __init__(self, name: str, cost: D = D(0)) -> None:
        self.name = name
        self.model = name
        self._cost = cost
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        return CompletionResult(f"{self.name}:{request.prompt}", Usage(10, 5), self.model,
                                self.name, self._cost)

    def estimate_cost(self, request: CompletionRequest) -> D:
        return self._cost


class BatchStub(SyncStub):
    """A provider that also completes in one batch — the Anthropic-slice shape."""

    def __init__(self, name: str, cost: D = D(0)) -> None:
        super().__init__(name, cost)
        self.batches = 0

    def estimate_batch_cost(self, request: CompletionRequest) -> D:
        return self._cost

    async def complete_batch(self, requests: list[CompletionRequest]) -> list[CompletionResult]:
        self.batches += 1
        return [
            CompletionResult(f"{self.name}:{r.prompt}", Usage(10, 5), self.model, self.name, self._cost)
            for r in requests
        ]


class EmptyStub(SyncStub):
    """A provider that answers — and bills — but returns no text, the reasoning-
    model-with-thinking-on failure that drained deepseek credits in production."""

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        return CompletionResult("", Usage(50, 2000), self.model, self.name, self._cost)


class ErroringStub(SyncStub):
    """A provider whose every call raises — a bad API key or a down endpoint."""

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        raise RuntimeError("401 Unauthorized")


def mix_of(*providers, weights=None, fallback=None):  # noqa: ANN002, ANN003, ANN201
    weights = weights or [1.0] * len(providers)
    return ProviderMix(list(providers), weights, fallback=fallback)


# ── Routing, results, accounting ────────────────────────────────────────────
def test_every_item_gets_a_result_from_its_routed_provider() -> None:
    a, b = SyncStub("a"), SyncStub("b")
    report = run(CatalogueRunner(mix_of(a, b)).run(items(40)))
    assert len(report.results) == 40
    assert report.by_provider["a"] + report.by_provider["b"] == 40
    # Each result came from the provider the mix routes that item to.
    mix = mix_of(a, b)
    for it in items(40):
        assert report.results[it.item_id].text.startswith(mix.choose(item_seed(it.item_id)).name)


def test_routing_is_deterministic_across_runs() -> None:
    first = run(CatalogueRunner(mix_of(SyncStub("a"), SyncStub("b"))).run(items(30)))
    second = run(CatalogueRunner(mix_of(SyncStub("a"), SyncStub("b"))).run(items(30)))
    assert {k: v.text for k, v in first.results.items()} == \
           {k: v.text for k, v in second.results.items()}


def test_total_cost_sums_every_completion() -> None:
    report = run(CatalogueRunner(mix_of(SyncStub("a", D("0.001")))).run(items(10)))
    assert report.total_cost == D("0.010")


# ── Batch path ──────────────────────────────────────────────────────────────
def test_batch_capable_provider_is_called_once_for_its_whole_slice() -> None:
    batch = BatchStub("anthropic")
    # Route everything to the batch provider (weight only on it).
    report = run(CatalogueRunner(mix_of(batch)).run(items(25)))
    assert len(report.results) == 25
    assert batch.batches == 1          # one batch call, not 25
    assert batch.calls == 0            # never the per-item path


def test_use_batch_false_falls_back_to_per_item_calls() -> None:
    batch = BatchStub("anthropic")
    report = run(CatalogueRunner(mix_of(batch), use_batch=False).run(items(5)))
    assert len(report.results) == 5
    assert batch.batches == 0 and batch.calls == 5


# ── Budget ──────────────────────────────────────────────────────────────────
def test_budget_declines_further_work_once_the_cap_is_reached() -> None:
    # A budget cap is a spend ceiling, not a reason to abort. The runner never
    # raises: within one run the whole group is dispatched before any of it is
    # billed, so enforcement is *between* runs — once spend has crossed the cap a
    # later run is declined item by item, and those items become failures the
    # build falls back to procedural. Letting the cap raise instead would throw
    # away every result and fail a build the design means to complete.
    # (Regression: prod run ktwww failed four tasks this way.)
    guard = BudgetGuard(D("0.005"))            # ~5 items at 0.001 each
    runner = CatalogueRunner(mix_of(SyncStub("a", D("0.001"))), budget=guard)
    first = run(runner.run(items(5)))          # 0.005 spent — right at the cap
    assert len(first.results) == 5 and guard.spent == D("0.005")
    second = run(runner.run(items(5)))         # now over it
    assert second.results == {}                # every item declined
    assert len(second.failures) == 5           # …as failures, nothing raised
    assert guard.spent == D("0.005")           # declined items are never billed


def test_within_budget_completes_and_tracks_spend() -> None:
    guard = BudgetGuard(D("1.00"))
    report = run(CatalogueRunner(mix_of(SyncStub("a", D("0.001"))), budget=guard).run(items(10)))
    assert len(report.results) == 10
    assert guard.spent == D("0.010")


# ── Failure isolation ───────────────────────────────────────────────────────
class Flaky(SyncStub):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if request.prompt == "p3":
            raise RuntimeError("model blip")
        return await super().complete(request)


def test_one_item_failing_does_not_sink_the_rest() -> None:
    report = run(CatalogueRunner(mix_of(Flaky("a"))).run(items(6)))
    assert len(report.results) == 5
    assert "item-3" in report.failures
    assert "model blip" in report.failures["item-3"]


# ── Empty-provider circuit breaker ──────────────────────────────────────────
def test_a_quarantined_provider_falls_back_to_procedural_by_default() -> None:
    """A model that only ever returns empty text (deepseek/qwen reasoning their
    token budget away) is quarantined after a streak of empties, then its share
    degrades to procedural — NOT onto the surviving models, which by default must
    never inherit a dead provider's share and risk draining the budget on the
    pricier one. It simply stops being paid to produce nothing."""
    bad, good = EmptyStub("bad", D("0.01")), SyncStub("good", D("0.001"))
    runner = CatalogueRunner(mix_of(bad, good, weights=[0.5, 0.5]),
                             empty_streak_limit=3)

    run(runner.run(items(40)))
    assert "bad" in runner._quarantined            # crossed the streak limit
    calls_before, good_before = bad.calls, good.calls

    second = run(runner.run(items(40, start=40)))
    assert bad.calls == calls_before               # never called again
    assert len(second.failures) > 0                # bad's share went procedural…
    assert len(second.results) < 40                # …not to good
    assert len(second.results) + len(second.failures) == 40
    assert all(r.provider == "good" for r in second.results.values())
    # good handled only its own share — it did not inherit bad's.
    assert good.calls - good_before == len(second.results)


def test_a_consistently_erroring_provider_is_quarantined_and_routed_around() -> None:
    """A bad API key or a down endpoint (every call raises) is as unusable as an
    all-empty provider — it too is quarantined, so its share routes through the
    fallback pool instead of erroring on every item forever. This is what makes a
    wrong key a valid way to test the fallback behaviour."""
    bad, good = ErroringStub("bad", D("0.01")), SyncStub("good", D("0.001"))
    runner = CatalogueRunner(
        mix_of(bad, good, weights=[1.0, 0.0], fallback=[("good", 100.0)]),
        empty_streak_limit=3)

    run(runner.run(items(20)))                     # errors quarantine bad
    assert "bad" in runner._quarantined
    calls_before = bad.calls

    second = run(runner.run(items(40, start=100)))
    assert bad.calls == calls_before               # never called again
    assert len(second.results) == 40               # its share went to the survivor
    assert all(r.provider == "good" for r in second.results.values())


def test_an_occasional_error_does_not_quarantine() -> None:
    """One transient error among good answers must not trip the breaker — the
    streak resets on any usable completion."""
    class Flaky(SyncStub):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.calls += 1
            if request.prompt in ("p2", "p7"):
                raise RuntimeError("transient")
            return await SyncStub.complete(self, request)

    runner = CatalogueRunner(mix_of(Flaky("a")), empty_streak_limit=3)
    run(runner.run(items(15)))
    assert runner._quarantined == set()


def test_a_budget_decline_is_not_counted_as_a_provider_error() -> None:
    """Declining an item for budget is not the provider failing — it must never
    contribute to the quarantine streak, or a tight budget would quarantine a
    perfectly healthy model. Enforcement is between runs, so the first run spends
    and the second is fully declined; the healthy provider must survive it."""
    guard = BudgetGuard(D("0.005"))
    good = SyncStub("good", D("0.001"))
    runner = CatalogueRunner(mix_of(good), budget=guard, empty_streak_limit=3)
    run(runner.run(items(5)))                      # spends 0.005, at the cap
    second = run(runner.run(items(40, start=100)))  # every item declined for budget
    assert len(second.failures) == 40 and second.results == {}
    assert runner._quarantined == set()            # budget declines never quarantine


def test_a_fallback_pool_sends_a_dead_providers_share_to_the_named_model() -> None:
    """A fallback pool naming a survivor keeps the LLM fill high — the dead
    provider's share is paid to the model the operator chose, not lost to
    procedural. (This replaces the old reroute_quarantined boolean.)"""
    bad, good = EmptyStub("bad", D("0.01")), SyncStub("good", D("0.001"))
    runner = CatalogueRunner(
        mix_of(bad, good, weights=[0.5, 0.5], fallback=[("good", 100.0)]),
        empty_streak_limit=3)

    run(runner.run(items(40)))
    assert "bad" in runner._quarantined
    calls_before = bad.calls

    second = run(runner.run(items(40, start=40)))
    assert bad.calls == calls_before               # never called again
    assert len(second.results) == 40               # all sent to the named survivor
    assert all(r.provider == "good" for r in second.results.values())
    assert second.failures == {}


def test_a_fallback_pool_splits_the_dead_share_by_configured_proportion() -> None:
    """The core of the feature: a dead provider's share is split across the pool
    in the configured ratio — here 70% to a survivor, 30% to procedural."""
    # good has weight 0, so everything normally routes to bad; once bad is
    # quarantined its whole share redistributes 70/30.
    bad, good = EmptyStub("bad", D("0.01")), SyncStub("good", D("0.001"))
    runner = CatalogueRunner(
        mix_of(bad, good, weights=[1.0, 0.0],
               fallback=[("good", 70.0), ("procedural", 30.0)]),
        empty_streak_limit=3)

    run(runner.run(items(60)))                     # quarantine bad
    assert "bad" in runner._quarantined

    second = run(runner.run(items(400, start=100)))
    filled, proc = len(second.results), len(second.failures)
    assert filled + proc == 400
    assert all(r.provider == "good" for r in second.results.values())
    assert 0.62 < filled / 400 < 0.78              # ~70% to good, ~30% procedural


def test_an_occasional_empty_does_not_quarantine() -> None:
    """One blank among good answers must not trip the breaker — the streak resets
    on any non-empty completion."""
    class Blip(SyncStub):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.calls += 1
            text = "" if request.prompt == "p2" else f"{self.name}:{request.prompt}"
            return CompletionResult(text, Usage(10, 5), self.model, self.name, self._cost)

    runner = CatalogueRunner(mix_of(Blip("a")), empty_streak_limit=3)
    run(runner.run(items(10)))
    assert runner._quarantined == set()


# ── Anthropic batch mapping (pure + fake client) ────────────────────────────
def test_build_batch_requests_carries_positional_custom_ids() -> None:
    reqs = [CompletionRequest(system="s", prompt=f"p{i}", max_tokens=64) for i in range(3)]
    built = build_batch_requests("claude-haiku-4-5", reqs)
    assert [b["custom_id"] for b in built] == ["item-0", "item-1", "item-2"]
    assert built[0]["params"]["model"] == "claude-haiku-4-5"
    assert built[0]["params"]["messages"][0]["content"] == "p0"
    assert all(batch_custom_id_index(b["custom_id"]) == i for i, b in enumerate(built))


def test_batch_is_half_the_synchronous_price() -> None:
    p = AnthropicProvider(model="claude-haiku-4-5", client=object())
    req = CompletionRequest(system="s", prompt="p", max_tokens=100)
    assert p.estimate_batch_cost(req) == p.estimate_cost(req) / 2


# A fake Message Batches API that returns results out of order, keyed by custom_id.
class _Usage:
    input_tokens = 500
    output_tokens = 90
    cache_read_input_tokens = 0


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Msg:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Result:
    def __init__(self, text: str) -> None:
        self.type = "succeeded"
        self.message = _Msg(text)


class _Entry:
    def __init__(self, custom_id: str, text: str) -> None:
        self.custom_id = custom_id
        self.result = _Result(text)


class _Batch:
    id = "batch_1"
    processing_status = "ended"


class _FakeBatches:
    def __init__(self) -> None:
        self._reqs = None

    async def create(self, requests):  # noqa: ANN001, ANN201
        self._reqs = requests
        return _Batch()

    async def retrieve(self, batch_id):  # noqa: ANN001, ANN201
        return _Batch()

    def results(self, batch_id):  # noqa: ANN001, ANN201 - out-of-order on purpose
        return [
            _Entry("item-2", "third"),
            _Entry("item-0", "first"),
            _Entry("item-1", "second"),
        ]


class _FakeBatchClient:
    def __init__(self) -> None:
        self.messages = type("M", (), {"batches": _FakeBatches()})()


def test_complete_batch_restores_input_order_and_batch_cost() -> None:
    provider = AnthropicProvider(model="claude-haiku-4-5", client=_FakeBatchClient())
    reqs = [CompletionRequest(system="s", prompt=t) for t in ("a", "b", "c")]
    results = run(provider.complete_batch(reqs, poll_interval=0))
    assert [r.text for r in results] == ["first", "second", "third"]   # reordered by custom_id
    # Half-price: 500 in @ $0.50/M + 90 out @ $2.50/M.
    assert results[0].cost == D("500") * D("0.50") / 1_000_000 + D("90") * D("2.50") / 1_000_000


# ── Empty completions are failures, not results ─────────────────────────────
class EmptyStub(SyncStub):
    """A reasoning model that spent its whole budget thinking: HTTP 200, real
    usage, real cost, and no text. Observed from deepseek-v4-flash."""

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        return CompletionResult("", Usage(37, 48), self.model, self.name, self._cost)


class BlankStub(SyncStub):
    """Whitespace-only — the same defect wearing a disguise."""

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        return CompletionResult("   \n  ", Usage(37, 48), self.model, self.name, self._cost)


def test_an_empty_completion_is_a_failure_not_a_result() -> None:
    """The bug that would have baked blank descriptions into a shipped catalogue
    while reporting the build clean."""
    mix = ProviderMix([EmptyStub("deepseek", D("0.001"))], [1.0])
    report = run(CatalogueRunner(mix).run(items(4)))
    assert report.results == {}
    assert len(report.failures) == 4
    assert "empty completion" in report.failures["item-0"]


def test_a_whitespace_only_completion_is_also_a_failure() -> None:
    mix = ProviderMix([BlankStub("qwen", D("0.001"))], [1.0])
    report = run(CatalogueRunner(mix).run(items(2)))
    assert report.results == {}
    assert len(report.failures) == 2


def test_an_empty_completion_still_costs_money() -> None:
    """You were billed for the tokens whether or not you got an answer, so the
    budget and the cost total must see it — only success differs."""
    budget = BudgetGuard(D("1.00"))
    mix = ProviderMix([EmptyStub("deepseek", D("0.01"))], [1.0])
    report = run(CatalogueRunner(mix, budget=budget).run(items(5)))
    assert report.total_cost == D("0.05")
    assert budget.spent == D("0.05")
    assert report.results == {}


def test_good_and_empty_completions_are_separated() -> None:
    """A mixed run must keep the usable results and reject only the blanks."""
    good, empty = SyncStub("good", D("0.001")), EmptyStub("empty", D("0.001"))
    mix = ProviderMix([good, empty], [0.5, 0.5])
    report = run(CatalogueRunner(mix).run(items(20)))
    assert report.results and report.failures
    assert len(report.results) + len(report.failures) == 20
    assert all(r.text for r in report.results.values())
