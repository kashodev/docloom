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

from docloom.core.providers.base import CompletionRequest, CompletionResult, Usage
from docloom.core.providers.pricing import Pricing, pricing_for

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
    ) -> None:
        self.name = name
        self.model = model
        self.pricing = pricing or pricing_for(model)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # A caller-supplied client is reused (and injected by tests); otherwise
        # one is created lazily on first use and owned by this provider.
        self._client = client
        self._owns_client = client is None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
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
        resp = await client.post(
            f"{self._base_url}/chat/completions", json=body, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        text = data["choices"][0]["message"]["content"]
        usage = _parse_usage(data.get("usage", {}))
        cost = self.pricing.cost(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
        return CompletionResult(
            text=text, usage=usage, model=self.model, provider=self.name, cost=cost
        )

    def estimate_cost(self, request: CompletionRequest) -> Decimal:
        est_input = (len(request.system) + len(request.prompt)) // _CHARS_PER_TOKEN
        return self.pricing.cost(est_input, request.max_tokens)

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
