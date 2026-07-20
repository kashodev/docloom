"""Provider-layer tests.

The OpenAI-compatible adapter is exercised end to end against an httpx mock
transport — real request building, real usage/cost parsing, no network, no keys.
The Anthropic adapter uses an injected fake client (its SDK is an extra and is
not installed). Pricing, the weighted mix's determinism and distribution, and
the budget guard are unit-tested directly.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

import httpx
import pytest

from docloom.core.providers import (
    BudgetExceeded,
    BudgetGuard,
    CompletionRequest,
    OpenAICompatibleProvider,
    Pricing,
    ProviderMix,
    build_mix,
    build_provider,
    pricing_for,
)
from docloom.core.providers.anthropic_provider import AnthropicProvider
from docloom.core.providers.base import CompletionResult, TextProvider, Usage


def run(coro):  # noqa: ANN001, ANN201
    """Drive a coroutine without pytest-asyncio."""
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Pricing
# ─────────────────────────────────────────────────────────────────────────────

def test_cost_is_exact_and_unquantised() -> None:
    """A single completion costs a fraction of a cent; it must not round to 0."""
    p = Pricing(D("0.14"), D("0.28"))
    # 1000 input + 200 output tokens: 1000*0.14/1e6 + 200*0.28/1e6
    assert p.cost(1000, 200) == D("0.00014") + D("0.000056")
    assert p.cost(1000, 200) > 0


def test_cached_tokens_use_the_cached_rate() -> None:
    p = Pricing(D("0.14"), D("0.28"), D("0.0028"))
    full = p.cost(1000, 0)                      # all input at full rate
    mostly_cached = p.cost(1000, 0, 900)        # 900 cached at 2% of the rate
    assert mostly_cached < full
    assert mostly_cached == 100 * D("0.14") / 1_000_000 + 900 * D("0.0028") / 1_000_000


def test_cached_rate_falls_back_to_input_rate_when_absent() -> None:
    p = Pricing(D("0.05"), D("0.40"))           # no cached rate (Qwen)
    assert p.cost(1000, 0, 500) == p.cost(1000, 0)


def test_unknown_model_is_free() -> None:
    assert pricing_for("some-local-model").cost(10_000, 10_000) == 0


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible provider — via mock transport
# ─────────────────────────────────────────────────────────────────────────────

def _mock_client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_openai_provider_parses_text_usage_and_cost() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "A rugged 18V cordless drill."}}],
            "usage": {"prompt_tokens": 850, "completion_tokens": 120},
        })

    provider = OpenAICompatibleProvider(
        "deepseek", "deepseek-v4-flash", "https://api.deepseek.com",
        api_key="sk-test", client=_mock_client(handler),
    )
    result = run(provider.complete(CompletionRequest(system="You write copy.", prompt="Drill.")))

    assert result.text == "A rugged 18V cordless drill."
    assert result.usage.input_tokens == 850
    assert result.usage.output_tokens == 120
    assert result.provider == "deepseek"
    assert result.cost == D("850") * D("0.14") / 1_000_000 + D("120") * D("0.28") / 1_000_000

    # Request was built correctly.
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert [m["role"] for m in captured["body"]["messages"]] == ["system", "user"]


def test_openai_provider_reads_deepseek_cache_hits() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                      "prompt_cache_hit_tokens": 900},
        })

    provider = OpenAICompatibleProvider(
        "deepseek", "deepseek-v4-flash", "https://api.deepseek.com",
        client=_mock_client(handler),
    )
    result = run(provider.complete(CompletionRequest(system="s", prompt="p")))
    assert result.usage.cached_input_tokens == 900


def test_openai_provider_reads_openai_style_cache_hits() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 300}},
        })

    provider = OpenAICompatibleProvider(
        "dashscope", "qwen3.5-flash", "https://x", client=_mock_client(handler),
    )
    result = run(provider.complete(CompletionRequest(system="s", prompt="p")))
    assert result.usage.cached_input_tokens == 300


def test_http_error_propagates() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    provider = OpenAICompatibleProvider(
        "deepseek", "deepseek-v4-flash", "https://x", client=_mock_client(handler),
    )
    with pytest.raises(httpx.HTTPStatusError):
        run(provider.complete(CompletionRequest(system="s", prompt="p")))


def test_provider_satisfies_protocol() -> None:
    p = OpenAICompatibleProvider("ollama", "__local__", "http://localhost:11434/v1")
    assert isinstance(p, TextProvider)


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic provider — injected fake client
# ─────────────────────────────────────────────────────────────────────────────

class _FakeAnthropicUsage:
    input_tokens = 500
    output_tokens = 90
    cache_read_input_tokens = 0


class _FakeBlock:
    type = "text"
    text = "Enterprise plan — unlimited seats."


class _FakeMessage:
    content = [_FakeBlock()]
    usage = _FakeAnthropicUsage()


class _FakeMessages:
    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.kwargs = kwargs
        return _FakeMessage()


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def test_anthropic_provider_via_fake_client() -> None:
    provider = AnthropicProvider(model="claude-haiku-4-5", client=_FakeAnthropicClient())
    result = run(provider.complete(CompletionRequest(system="s", prompt="p", max_tokens=256)))
    assert result.text == "Enterprise plan — unlimited seats."
    assert result.provider == "anthropic"
    assert result.usage.input_tokens == 500
    assert result.cost == D("500") * D("1.00") / 1_000_000 + D("90") * D("5.00") / 1_000_000


def test_anthropic_provider_without_sdk_gives_actionable_error() -> None:
    # anthropic is not installed in the test env; constructing without an
    # injected client must name the extra.
    with pytest.raises(ImportError, match=r"docloom\[anthropic\]"):
        AnthropicProvider(model="claude-haiku-4-5")


# ─────────────────────────────────────────────────────────────────────────────
# ProviderMix — determinism and distribution
# ─────────────────────────────────────────────────────────────────────────────

class StubProvider:
    """A zero-cost provider that echoes its name, for routing tests."""

    pricing = pricing_for("__local__")

    def __init__(self, name: str) -> None:
        self.name = name
        self.model = name

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(self.name, Usage(0, 0), self.model, self.name, D(0))

    def estimate_cost(self, request: CompletionRequest) -> D:
        return D(0)


def a_mix(**kw) -> ProviderMix:  # noqa: ANN003
    return ProviderMix(
        [StubProvider("deepseek"), StubProvider("dashscope"), StubProvider("anthropic")],
        [0.4, 0.4, 0.2],
        **kw,
    )


def test_same_seed_routes_to_the_same_provider() -> None:
    mix = a_mix()
    assert mix.choose(12345).name == mix.choose(12345).name


def test_weighted_distribution_approximates_the_split() -> None:
    mix = a_mix()
    from collections import Counter
    counts = Counter(mix.choose(seed).name for seed in range(20_000))
    frac = {k: v / 20_000 for k, v in counts.items()}
    assert abs(frac["deepseek"] - 0.4) < 0.02
    assert abs(frac["dashscope"] - 0.4) < 0.02
    assert abs(frac["anthropic"] - 0.2) < 0.02


def test_mix_rejects_mismatched_weights() -> None:
    with pytest.raises(ValueError, match="same length"):
        ProviderMix([StubProvider("a")], [0.5, 0.5])


def test_mix_complete_routes_by_seed() -> None:
    mix = a_mix()
    result = run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))
    assert result.provider == mix.choose(1).name


# ─────────────────────────────────────────────────────────────────────────────
# BudgetGuard
# ─────────────────────────────────────────────────────────────────────────────

def test_budget_accumulates_and_reports_remaining() -> None:
    g = BudgetGuard(D("1.00"))
    g.add(D("0.30"))
    g.add(D("0.20"))
    assert g.spent == D("0.50")
    assert g.remaining == D("0.50")


def test_budget_check_blocks_before_overspending() -> None:
    g = BudgetGuard(D("0.10"))
    g.add(D("0.09"))
    with pytest.raises(BudgetExceeded, match="would exceed"):
        g.check(D("0.05"))


def test_budget_add_raises_when_over() -> None:
    g = BudgetGuard(D("0.10"))
    with pytest.raises(BudgetExceeded, match="budget exceeded"):
        g.add(D("0.11"))


def test_budget_off_does_not_gate() -> None:
    g = BudgetGuard(D("0.10"), abort_on_exceed=False)
    g.check(D("999"))       # no raise
    g.add(D("999"))         # no raise
    assert g.spent == D("999")


def test_mix_enforces_budget() -> None:
    mix = a_mix(budget=BudgetGuard(D("0.00"), abort_on_exceed=True))
    # Stub providers cost 0, so a 0 budget is fine.
    result = run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))
    assert result.cost == 0


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def test_build_mix_from_config() -> None:
    config = {
        "text": [
            {"name": "deepseek", "model": "deepseek-v4-flash", "weight": 0.4},
            {"name": "dashscope", "model": "qwen3.5-flash", "weight": 0.4},
            {"name": "ollama", "model": "__local__", "weight": 0.2},
        ],
        "budget": {"limit_usd": 50, "abort_on_exceed": True},
    }
    mix = build_mix(config)
    assert [p.name for p in mix.providers] == ["deepseek", "dashscope", "ollama"]


def test_build_provider_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider({"name": "nope", "model": "x"})
