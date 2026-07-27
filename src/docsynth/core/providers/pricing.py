"""Model pricing.

A small table of per-million-token rates, and the arithmetic to turn token
usage into a cost. Rates are cached here for convenience and budgeting; they
move, so a config may override any of them. Costs stay in raw ``Decimal`` — a
completion can cost far less than a cent, so nothing is quantised.

Prices below are per 1M tokens, USD, as of the project's research snapshot
(mid-2026). Verify before a large run; they are the thing most likely to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-million-token USD rates for one model."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None   # falls back to input rate

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> Decimal:
        """Cost of one completion, exact, unquantised.

        Cached prompt tokens are billed at the cached rate when the provider
        exposes one (DeepSeek's cache-hit rate is ~2% of its input rate); the
        remaining prompt tokens and all output tokens at the standard rates.
        """
        cached_rate = self.cached_input_per_mtok
        uncached = max(input_tokens - cached_input_tokens, 0)
        total = uncached * self.input_per_mtok + output_tokens * self.output_per_mtok
        if cached_input_tokens:
            total += cached_input_tokens * (
                cached_rate if cached_rate is not None else self.input_per_mtok
            )
        return total / _MILLION


def _d(value: str) -> Decimal:
    return Decimal(value)


# Cached snapshot. A provider spec may override via explicit rates in config.
PRICING: dict[str, Pricing] = {
    # DeepSeek V4-Flash — cache-hit input is ~2% of the cache-miss rate.
    "deepseek-v4-flash": Pricing(_d("0.14"), _d("0.28"), _d("0.0028")),
    # Qwen3.5-Flash (Alibaba DashScope) — bulk tier, ≤256K prompts.
    "qwen3.5-flash": Pricing(_d("0.05"), _d("0.40")),
    # Claude Haiku 4.5.
    "claude-haiku-4-5": Pricing(_d("1.00"), _d("5.00")),
    # Local models via Ollama — free.
    "__local__": Pricing(_d("0"), _d("0"), _d("0")),
}


def pricing_for(model: str) -> Pricing:
    """Look up a model's pricing, defaulting to free for local/unknown models.

    An unknown model returns free pricing rather than raising: a local or
    self-hosted model legitimately has no list price, and a budget of $0 for an
    unpriced model is the safe default (it simply won't be gated on cost).
    """
    return PRICING.get(model, PRICING["__local__"])
