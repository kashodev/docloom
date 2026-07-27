"""Weighted provider mix.

The weighted provider split (e.g. 40/40/20 DeepSeek/Qwen/Haiku), expressed as data. Two
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

#: The literal a `catalogue.fallback` entry uses to send a share to the free
#: procedural description instead of another model.
PROCEDURAL_TARGET = "procedural"


class _Procedural:
    """Sentinel returned by :meth:`ProviderMix.route` for "no provider — use the
    procedural description". A distinct object so callers test it with ``is``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "PROCEDURAL"


#: Route sentinel: this item has no live model and falls back to procedural.
PROCEDURAL = _Procedural()

#: Decorrelates the fallback draw from the primary provider draw so the two
#: distributions are independent while both stay a pure function of the seed.
_FALLBACK_SALT = 0x9E3779B1


class ProviderMix:
    """A weighted ensemble of providers with deterministic per-item routing."""

    def __init__(
        self,
        providers: list[TextProvider],
        weights: list[float],
        *,
        budget: BudgetGuard | None = None,
        usage: UsageSink | None = None,
        fallback: list[tuple[str, float]] | None = None,
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

        # Fallback pool: how a quarantined provider's share is redistributed.
        # Each entry targets a provider already in the mix or the literal
        # `procedural` (the free sink). Empty ⇒ a quarantined share goes
        # straight to procedural — the safe default. Shares are normalised here.
        self._by_name = {p.name: p for p in providers}
        self._fallback: list[tuple[str, float]] = []
        if fallback:
            unknown = [t for t, _ in fallback
                       if t != PROCEDURAL_TARGET and t not in self._by_name]
            if unknown:
                raise ValueError(
                    f"fallback target(s) {unknown} are neither {PROCEDURAL_TARGET!r} "
                    f"nor a provider in the mix ({sorted(self._by_name)})")
            ftotal = sum(share for _, share in fallback)
            if ftotal <= 0:
                raise ValueError("fallback shares must sum to a positive number")
            self._fallback = [(t, s / ftotal) for t, s in fallback]

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

    def route(
        self, seed: int, *, quarantined: frozenset[str] = frozenset()
    ) -> TextProvider | _Procedural:
        """Route an item to a live provider, or to :data:`PROCEDURAL`.

        The item's provider is chosen normally (by weight, from ``seed``). If that
        provider is quarantined, its share is redistributed across the configured
        **fallback pool** — never silently onto the surviving mix by weight, which
        could land a dead cheap model's work on the priciest survivor. With no
        fallback pool the share goes to procedural (the safe default). A fallback
        entry that is itself quarantined is dropped and its share renormalised
        over the rest; a ``procedural`` entry, or an exhausted pool, yields
        :data:`PROCEDURAL`. Deterministic in ``(seed, quarantined)``.
        """
        chosen = self.choose(seed)
        if chosen.name not in quarantined:
            return chosen
        live = [(t, s) for t, s in self._fallback
                if t == PROCEDURAL_TARGET or t not in quarantined]
        if not live:
            return PROCEDURAL
        total = sum(s for _, s in live)
        point = Random(seed ^ _FALLBACK_SALT).random()
        acc = 0.0
        target = live[-1][0]                       # float-rounding tail default
        for name, s in live:
            acc += s / total
            if point < acc:
                target = name
                break
        return PROCEDURAL if target == PROCEDURAL_TARGET else self._by_name[target]

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
