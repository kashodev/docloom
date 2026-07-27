"""OpenAI-compatible chat provider.

Covers DeepSeek and Qwen (Alibaba DashScope) — both speak the OpenAI
``/chat/completions`` wire protocol — and Ollama for local models. One adapter,
three configurations, so the bulk of the catalogue mix rides a single tested
code path.

The httpx client is injected, so tests drive the adapter with a mock transport:
no network, no keys, but the real request-building and usage/cost parsing.

Anthropic is intentionally NOT routed through here. Claude has a first-party SDK
and should use it, not an OpenAI-compatibility shim — see
``anthropic_provider.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from docsynth.core.providers.base import CompletionRequest, CompletionResult, Usage
from docsynth.core.providers.pricing import Pricing, pricing_for

# ~4 characters per token is the standard rough English heuristic. Only used for
# pre-flight budgeting, never for billing.
_CHARS_PER_TOKEN = 4


class OpenAICompatibleProvider:
    """A model behind an OpenAI-style ``/chat/completions`` endpoint."""

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        *,
        api_key: str | None = None,
        pricing: Pricing | None = None,
        client: httpx.AsyncClient | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout_s: float = 45.0,
    ) -> None:
        self.name = name
        self.model = model
        self.pricing = pricing or pricing_for(model)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # A single short description is seconds of work; 120s was a trap. A model
        # that holds the connection open (a throttled/tarpitted key, a stuck
        # endpoint) otherwise ties up a concurrency slot for two minutes a call,
        # and a whole unit's worth of that runs a task out of its wall-clock
        # budget before the circuit breaker can quarantine it. Fail fast instead.
        self._timeout_s = timeout_s
        # Merged into every request body, overriding the defaults built here.
        # A deliberate passthrough rather than named options: the parameters that
        # matter are provider-specific and this adapter serves several. DashScope
        # disables Qwen's thinking with `enable_thinking: false`; without it,
        # qwen3.5-flash ignored `max_tokens` entirely and spent 2,335 tokens
        # reasoning before a ~24-token answer. Encoding that as a docsynth-level
        # option would mean teaching the kernel each vendor's vocabulary.
        self._extra_body = dict(extra_body or {})
        # Largest output this model has actually returned, used to floor the
        # pre-flight estimate (see `estimate_cost`). Plain attribute rather than
        # a lock: this adapter is driven from one asyncio loop, where an integer
        # assignment cannot be interleaved.
        self._observed_max_output = 0
        # A caller-supplied client is reused (and injected by tests); otherwise
        # one is created lazily on first use and owned by this provider.
        self._client = client
        self._owns_client = client is None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s))
        return self._client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        client = await self._ensure_client()
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }
        # Last, so a configured parameter can override a default — including
        # `max_tokens` itself, for endpoints that want `max_completion_tokens`.
        body.update(self._extra_body)
        resp = await client.post(
            f"{self._base_url}/chat/completions", json=body, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        # `content` is null, not absent, when a reasoning model exhausts its
        # token budget before producing an answer — so coalesce rather than
        # letting None propagate into a record. The runner rejects empty text as
        # a failure; see CatalogueRunner._record.
        text = data["choices"][0]["message"].get("content") or ""
        usage = _parse_usage(data.get("usage", {}))
        self._observed_max_output = max(self._observed_max_output, usage.output_tokens)
        cost = self.pricing.cost(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
        return CompletionResult(
            text=text, usage=usage, model=self.model, provider=self.name, cost=cost
        )

    def estimate_cost(self, request: CompletionRequest) -> Decimal:
        """Pre-flight estimate, floored by what this model has actually produced.

        Estimating output as ``max_tokens`` assumes the model honours its own
        cap. Qwen did not: asked for 48 it returned 2,359, so every estimate was
        ~50× low. That matters most on the *batch* path, where one aggregate
        estimate approves a whole batch in a single call — the per-call path is
        backstopped by :meth:`BudgetGuard.add`, which enforces on real spend, but
        a batch can overshoot before any actual cost is recorded.

        So the estimate never under-predicts against evidence: once a response
        has exceeded ``max_tokens``, later estimates assume at least that much.
        Self-correcting after one call, and conservative in the direction that
        protects the budget.
        """
        est_input = (len(request.system) + len(request.prompt)) // _CHARS_PER_TOKEN
        est_output = max(request.max_tokens, self._observed_max_output)
        return self.pricing.cost(est_input, est_output)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()


def _parse_usage(usage: dict[str, Any]) -> Usage:
    """Parse token usage across the several shapes providers use.

    OpenAI/Qwen nest cache hits under ``prompt_tokens_details.cached_tokens``;
    DeepSeek reports ``prompt_cache_hit_tokens`` at the top level. Both are
    handled so the cached rate applies wherever it should.
    """
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    cached = int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        or 0
    )
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached)
