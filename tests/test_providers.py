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
from typing import ClassVar

import httpx
import pytest

from docsynth.core.providers import (
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
from docsynth.core.providers.anthropic_provider import AnthropicProvider
from docsynth.core.providers.base import CompletionResult, TextProvider, Usage


def run(coro):
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

def _mock_client(handler) -> httpx.AsyncClient:
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
    content: ClassVar = [_FakeBlock()]
    usage = _FakeAnthropicUsage()


class _FakeMessages:
    async def create(self, **kwargs):
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


def _anthropic_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("anthropic") is not None


@pytest.mark.skipif(
    _anthropic_installed(),
    reason="tests the SDK-absent path; the anthropic extra is present here",
)
def test_anthropic_provider_without_sdk_gives_actionable_error() -> None:
    # Only meaningful when the extra is absent (the default core-only env):
    # constructing without an injected client must name the extra rather than
    # raising a bare ModuleNotFoundError. Skipped once anthropic is installed —
    # e.g. after an LLM catalogue build or smoke run in the same venv.
    with pytest.raises(ImportError, match=r"docsynth\[anthropic\]"):
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


def a_mix(**kw) -> ProviderMix:
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


# ── Fallback routing (quarantine → fallback pool) ────────────────────────────
def test_route_returns_the_chosen_provider_when_none_are_quarantined() -> None:
    mix = a_mix()
    for seed in range(200):
        assert mix.route(seed) is mix.choose(seed)          # identical to normal


def test_a_quarantined_share_goes_to_procedural_with_no_fallback_pool() -> None:
    from docsynth.core.providers.mix import PROCEDURAL
    mix = a_mix()                                           # no fallback configured
    # Every item whose chosen provider is quarantined must degrade to procedural.
    q = frozenset({"deepseek", "dashscope", "anthropic"})
    assert all(mix.route(seed, quarantined=q) is PROCEDURAL for seed in range(200))


def test_route_distributes_a_dead_share_across_the_fallback_pool() -> None:
    from docsynth.core.providers.mix import PROCEDURAL
    mix = a_mix(fallback=[("anthropic", 60.0), ("procedural", 40.0)])
    dead = frozenset({"deepseek"})
    # Look only at the items deepseek WOULD have served; their share splits 60/40.
    deepseek_seeds = [s for s in range(20_000) if mix.choose(s).name == "deepseek"]
    to_proc = sum(1 for s in deepseek_seeds
                  if mix.route(s, quarantined=dead) is PROCEDURAL)
    assert deepseek_seeds                                   # sanity: some were deepseek's
    assert abs(to_proc / len(deepseek_seeds) - 0.4) < 0.03  # ~40% → procedural
    # The rest went to the named fallback model, never back to the dead one.
    assert all(mix.route(s, quarantined=dead) is PROCEDURAL
               or mix.route(s, quarantined=dead).name == "anthropic"
               for s in deepseek_seeds)


def test_route_cascades_past_a_quarantined_fallback_member() -> None:
    from docsynth.core.providers.mix import PROCEDURAL
    # Fallback pool is entirely dashscope; if dashscope is ALSO dead the share
    # cascades to procedural rather than to a quarantined model.
    mix = a_mix(fallback=[("dashscope", 100.0)])
    both_dead = frozenset({"deepseek", "dashscope"})
    for seed in range(500):
        if mix.choose(seed).name == "deepseek":
            assert mix.route(seed, quarantined=both_dead) is PROCEDURAL


def test_route_is_deterministic_in_seed_and_quarantined() -> None:
    mix = a_mix(fallback=[("anthropic", 50.0), ("procedural", 50.0)])
    dead = frozenset({"deepseek"})
    for seed in range(300):
        a = mix.route(seed, quarantined=dead)
        b = mix.route(seed, quarantined=dead)
        assert a is b


def test_fallback_rejects_an_unknown_target() -> None:
    with pytest.raises(ValueError, match="neither 'procedural'"):
        a_mix(fallback=[("nope", 100.0)])


def test_fallback_rejects_nonpositive_shares() -> None:
    with pytest.raises(ValueError, match="sum to a positive"):
        a_mix(fallback=[("anthropic", 0.0)])


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


def test_build_mix_parses_a_fallback_pool() -> None:
    from docsynth.core.providers.mix import PROCEDURAL
    config = {
        "text": [
            {"name": "deepseek", "model": "deepseek-v4-flash", "weight": 0.5},
            {"name": "dashscope", "model": "qwen3.5-flash", "weight": 0.5},
        ],
        "fallback": [{"name": "dashscope", "share": 70}, {"name": "procedural", "share": 30}],
    }
    mix = build_mix(config)
    # A deepseek-routed seed, with deepseek quarantined, lands on dashscope or
    # procedural — never back on deepseek.
    dead = frozenset({"deepseek"})
    for seed in range(300):
        if mix.choose(seed).name == "deepseek":
            r = mix.route(seed, quarantined=dead)
            assert r is PROCEDURAL or r.name == "dashscope"


def test_build_provider_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider({"name": "nope", "model": "x"})


def test_provider_timeout_defaults_low_and_is_configurable() -> None:
    """120s was a trap — a hung call ties up a slot until it. Default is short,
    and an operator can tune it per provider from config."""
    from docsynth.core.providers.openai_compatible import OpenAICompatibleProvider
    assert OpenAICompatibleProvider(name="x", model="m", base_url="http://h")._timeout_s == 45.0
    tuned = build_provider({"name": "deepseek", "model": "m", "timeout_s": 20})
    assert tuned._timeout_s == 20.0


# ── Reasoning models (regressions from a real smoke run) ────────────────────
def _reasoning_response(*, content, completion_tokens, capture=None):
    """A DeepSeek/Qwen-shaped reply where the model spent its budget thinking."""
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            import json
            capture.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content,
                                     "reasoning_content": "thinking…" * 20},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 37, "completion_tokens": completion_tokens,
                      "completion_tokens_details": {"reasoning_tokens": completion_tokens}},
        })
    return handler


def test_null_content_does_not_propagate_as_none() -> None:
    """DeepSeek returns `content: null` — not absent, not "" — when reasoning
    consumes the whole token budget. Letting None through would put a null into
    a catalogue record."""
    provider = OpenAICompatibleProvider(
        name="deepseek", model="deepseek-v4-flash", base_url="https://x",
        client=_mock_client(_reasoning_response(content=None, completion_tokens=48)),
    )
    result = asyncio.run(provider.complete(CompletionRequest(system="s", prompt="p")))
    assert result.text == ""
    assert result.usage.output_tokens == 48      # billed for it regardless


def test_extra_body_is_merged_into_the_request() -> None:
    """How thinking gets disabled: DashScope takes `enable_thinking: false`.
    A passthrough, so the kernel never learns each vendor's vocabulary."""
    seen: list[dict] = []
    provider = OpenAICompatibleProvider(
        name="dashscope", model="qwen3.5-flash", base_url="https://x",
        client=_mock_client(_reasoning_response(content="ok", completion_tokens=20,
                                                capture=seen)),
        extra_body={"enable_thinking": False},
    )
    asyncio.run(provider.complete(CompletionRequest(system="s", prompt="p")))
    assert seen[0]["enable_thinking"] is False
    assert seen[0]["model"] == "qwen3.5-flash"


def test_extra_body_can_override_a_default() -> None:
    """Some endpoints want `max_completion_tokens` instead of `max_tokens`."""
    seen: list[dict] = []
    provider = OpenAICompatibleProvider(
        name="dashscope", model="qwen3.5-flash", base_url="https://x",
        client=_mock_client(_reasoning_response(content="ok", completion_tokens=5,
                                                capture=seen)),
        extra_body={"max_tokens": 999},
    )
    asyncio.run(provider.complete(CompletionRequest(system="s", prompt="p", max_tokens=48)))
    assert seen[0]["max_tokens"] == 999


def test_the_estimate_learns_from_output_that_broke_the_cap() -> None:
    """Qwen returned 2,359 tokens against max_tokens=48, so every pre-flight
    estimate was ~50x low. The estimate must never under-predict against
    evidence — it is what sizes a *batch*, which `BudgetGuard.add` cannot
    backstop."""
    provider = OpenAICompatibleProvider(
        name="dashscope", model="qwen3.5-flash", base_url="https://x",
        client=_mock_client(_reasoning_response(content="ok", completion_tokens=2359)),
    )
    request = CompletionRequest(system="s", prompt="p", max_tokens=48)
    before = provider.estimate_cost(request)
    asyncio.run(provider.complete(request))
    after = provider.estimate_cost(request)
    assert after > before * 10, f"estimate did not learn: {before} -> {after}"
