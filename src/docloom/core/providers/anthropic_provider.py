"""Anthropic (Claude) provider.

Uses the first-party ``anthropic`` SDK rather than an OpenAI-compatibility shim,
because Claude should be called through its own client. The SDK is an optional
extra (``docloom[anthropic]``), lazy-imported and client-injectable — so this
module imports with the SDK absent and is tested against a fake client.

In the catalogue mix this is the ~20% "diversity" slice: a different model's
voice mixed into the corpus so an extraction pipeline cannot learn a single
generator's style. See DESIGN.md.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from docloom.core.providers.base import CompletionRequest, CompletionResult, Usage
from docloom.core.providers.pricing import Pricing, pricing_for

_CHARS_PER_TOKEN = 4


class AnthropicProvider:
    """A Claude model via the Anthropic Messages API."""

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        *,
        api_key: str | None = None,
        pricing: Pricing | None = None,
        client: Any | None = None,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        self.pricing = pricing or pricing_for(model)
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "the anthropic provider needs its extra — pip install 'docloom[anthropic]'"
                ) from exc
            client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self._client = client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = Usage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cached_input_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        )
        cost = self.pricing.cost(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
        return CompletionResult(
            text=text, usage=usage, model=self.model, provider=self.name, cost=cost
        )

    def estimate_cost(self, request: CompletionRequest) -> Decimal:
        est_input = (len(request.system) + len(request.prompt)) // _CHARS_PER_TOKEN
        return self.pricing.cost(est_input, request.max_tokens)
