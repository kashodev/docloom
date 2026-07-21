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

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any

from docloom.core.providers.base import CompletionRequest, CompletionResult, Usage
from docloom.core.providers.pricing import Pricing, pricing_for

_CHARS_PER_TOKEN = 4
#: The Message Batches API bills at half the synchronous rate. The catalogue's
#: Haiku "diversity" slice is offline and latency-tolerant, so it goes through
#: batches — same tokens, half the cost.
_BATCH_DISCOUNT = Decimal("0.5")


def build_batch_requests(model: str, requests: list[CompletionRequest]) -> list[dict[str, Any]]:
    """Map completion requests to Message Batches ``requests`` entries.

    Each carries a ``custom_id`` of its position, so results — which come back in
    arbitrary order — can be restored to the input order. Pure, so this mapping
    is unit-tested without the SDK.
    """
    return [
        {
            "custom_id": f"item-{i}",
            "params": {
                "model": model,
                "max_tokens": r.max_tokens,
                "system": r.system,
                "messages": [{"role": "user", "content": r.prompt}],
            },
        }
        for i, r in enumerate(requests)
    ]


def batch_custom_id_index(custom_id: str) -> int:
    """Recover the input position from a ``custom_id`` (``"item-3"`` -> ``3``)."""
    return int(custom_id.rsplit("-", 1)[1])


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

    @property
    def batch_pricing(self) -> Pricing:
        """Half-price rates for the Message Batches API."""
        p = self.pricing
        cached = p.cached_input_per_mtok
        return replace(
            p,
            input_per_mtok=p.input_per_mtok * _BATCH_DISCOUNT,
            output_per_mtok=p.output_per_mtok * _BATCH_DISCOUNT,
            cached_input_per_mtok=cached * _BATCH_DISCOUNT if cached is not None else None,
        )

    def estimate_batch_cost(self, request: CompletionRequest) -> Decimal:
        est_input = (len(request.system) + len(request.prompt)) // _CHARS_PER_TOKEN
        return self.batch_pricing.cost(est_input, request.max_tokens)

    async def complete_batch(
        self, requests: list[CompletionRequest], *, poll_interval: float = 5.0
    ) -> list[CompletionResult]:
        """Complete many requests through the Message Batches API (half price).

        Submits one batch, polls until it ends, then maps each result back to its
        input position via ``custom_id``. Costs use the batch rates. Results are
        returned in input order. Injecting a client makes the whole flow testable
        against a fake batches API.
        """
        if not requests:
            return []
        batch = await self._client.messages.batches.create(
            requests=build_batch_requests(self.model, requests)
        )
        while True:
            status = await self._client.messages.batches.retrieve(batch.id)
            if status.processing_status == "ended":
                break
            await asyncio.sleep(poll_interval)

        ordered: list[CompletionResult | None] = [None] * len(requests)
        async for entry in self._iter_results(batch.id):
            if entry.result.type != "succeeded":
                continue   # errored/expired/cancelled entries are left as gaps
            message = entry.result.message
            usage = Usage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                cached_input_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            )
            text = "".join(b.text for b in message.content if b.type == "text")
            ordered[batch_custom_id_index(entry.custom_id)] = CompletionResult(
                text=text,
                usage=usage,
                model=self.model,
                provider=self.name,
                cost=self.batch_pricing.cost(
                    usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
                ),
            )
        missing = [i for i, r in enumerate(ordered) if r is None]
        if missing:
            raise RuntimeError(f"batch {batch.id} returned no result for items {missing}")
        return [r for r in ordered if r is not None]

    async def _iter_results(self, batch_id: str) -> Any:
        """Yield batch result entries, tolerating a sync- or async-iterable SDK."""
        results = self._client.messages.batches.results(batch_id)
        if hasattr(results, "__await__"):
            results = await results
        if hasattr(results, "__aiter__"):
            async for entry in results:
                yield entry
        else:
            for entry in results:
                yield entry
