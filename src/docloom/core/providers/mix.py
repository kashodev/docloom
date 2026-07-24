"""Weighted provider mix.

The 40/40/20 DeepSeek/Qwen/Haiku split from DESIGN.md, expressed as data. Two
properties matter:

* **Deterministic per item.** A catalogue item's provider is chosen from a seed
  derived from the item, so the same item always draws the same provider. That
  makes a run reproducible and lets a *slice* be regenerated (e.g. a weak set of
  descriptions) without reshuffling which model wrote what.

* **The mix is a quality decision, not a cost one.** Blending models mixes their
  voices so an extraction pipeline cannot learn a single generator's style —
  which is why the pricier Haiku slice is worth its share.

Selection is weighted by cumulative distribution: normalise the weights, walk a
seed-derived point in [0,1) across the cumulative bands. Optionally routes each
completion through a :class:`~docloom.core.providers.budget.BudgetGuard`.
"""

from __future__ import annotations

import time
from random import Random

from docloom.core.providers.base import CompletionRequest, CompletionResult, TextProvider
from docloom.core.providers.budget import BudgetGuard
from docloom.core.usage.base import LlmUsage, UsageSink


class ProviderMix:
    """A weighted ensemble of providers with deterministic per-item routing."""

    def __init__(
        self,
        providers: list[TextProvider],
        weights: list[float],
        *,
        budget: BudgetGuard | None = None,
        usage: UsageSink | None = None,
    ) -> None:
        if len(providers) != len(weights):
            raise ValueError("providers and weights must be the same length")
        if not providers:
            raise ValueError("a mix needs at least one provider")
        total = sum(weights)
        if total <= 0:
            raise ValueError("weights must sum to a positive number")
        self._providers = providers
        # Normalised weights, and their cumulative bands: [0.4, 0.8, 1.0] for
        # 40/40/20. The weights are kept so a subset can be renormalised on the
        # fly when a provider is excluded (a quarantined, all-empty model).
        self._weights = [w / total for w in weights]
        acc = 0.0
        self._cumulative: list[float] = []
        for w in self._weights:
            acc += w
            self._cumulative.append(acc)
        self._budget = budget
        self._usage = usage

    @property
    def providers(self) -> list[TextProvider]:
        return self._providers

    def choose(self, seed: int, *, exclude: frozenset[str] = frozenset()) -> TextProvider:
        """Pick a provider deterministically from ``seed``.

        ``exclude`` names providers to route *around* — a caller quarantines a
        model that is returning nothing but empty completions, and its share is
        renormalised across the survivors so their relative weights are kept. If
        every provider is excluded there is no one left to route around, so the
        full mix is used (the caller decides what to do with the result). The
        no-exclude path is unchanged, so an ordinary run stays bit-for-bit
        reproducible.
        """
        point = Random(seed).random()
        if exclude:
            live = [(p, w) for p, w in zip(self._providers, self._weights, strict=True)
                    if p.name not in exclude]
            if live:
                total = sum(w for _, w in live)
                acc = 0.0
                for provider, w in live:
                    acc += w / total
                    if point < acc:
                        return provider
                return live[-1][0]
        for provider, ceiling in zip(self._providers, self._cumulative, strict=True):
            if point < ceiling:
                return provider
        return self._providers[-1]   # guard against float rounding at 1.0

    async def complete(self, request: CompletionRequest, *, seed: int) -> CompletionResult:
        """Route one request to its seed-chosen provider, honouring the budget.

        Usage is recorded around the call — including failures, which are often
        still billed and always worth knowing about. Recording buffers in memory;
        nothing here touches I/O, so the call path is not slowed by telemetry.
        """
        provider = self.choose(seed)
        if self._budget is not None:
            self._budget.check(provider.estimate_cost(request))
        started = time.perf_counter()
        try:
            result = await provider.complete(request)
        except Exception as exc:
            if self._usage is not None:
                self._usage.record(LlmUsage.failure(
                    provider=provider.name, model=provider.model,
                    error=repr(exc), metadata=request.metadata,
                ))
            raise
        if self._usage is not None:
            self._usage.record(LlmUsage.from_completion(
                result,
                metadata=request.metadata,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
        if self._budget is not None:
            # Pass the model so a distributed guard can attribute spend
            # per model in the rollup; the in-process guard ignores it.
            self._budget.add(result.cost, model=result.model)
        return result
