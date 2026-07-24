"""Catalogue runner — drive a provider mix over many items, under a budget.

The one-time, offline catalogue step: tens of thousands of content items (product
descriptions, company blurbs), each routed to a model by the weighted
:class:`~docloom.core.providers.mix.ProviderMix`, generated within a hard dollar
:class:`~docloom.core.providers.budget.BudgetGuard`. Two things make it more than
a for-loop:

* **Batch where it pays.** Items whose chosen provider exposes ``complete_batch``
  (the Anthropic Haiku slice) are submitted together through the Message Batches
  API at half price, rather than one synchronous call each. Everything else runs
  concurrently with bounded parallelism.
* **Deterministic routing.** Each item's provider is chosen from a stable hash of
  its id, so a rerun draws the same model for the same item and a weak *slice* can
  be regenerated without reshuffling who wrote what — the mix's reproducibility
  guarantee, applied per catalogue item.

The runner owns budget accounting for both paths (pre-flight estimate check, then
actual cost), so pass the ``BudgetGuard`` here rather than to the mix.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from docloom.core.logging import get_logger
from docloom.core.providers.base import CompletionRequest, CompletionResult, TextProvider
from docloom.core.providers.budget import BudgetExceeded, BudgetGuard
from docloom.core.providers.mix import PROCEDURAL, ProviderMix
from docloom.core.usage.base import LlmUsage, UsageSink

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogueItem:
    """One content ask, identified so its result can be correlated and its
    provider chosen deterministically."""

    item_id: str
    request: CompletionRequest


@dataclass(slots=True)
class RunReport:
    """The outcome of a catalogue run."""

    results: dict[str, CompletionResult] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    by_provider: Counter[str] = field(default_factory=Counter)
    total_cost: Decimal = Decimal(0)


def item_seed(item_id: str) -> int:
    """A process-stable seed for an item id (SHA-256, not the salted ``hash``)."""
    return int.from_bytes(hashlib.sha256(item_id.encode("utf-8")).digest()[:8], "big")


class CatalogueRunner:
    """Runs catalogue items through a provider mix, batching where possible."""

    def __init__(
        self,
        mix: ProviderMix,
        *,
        budget: BudgetGuard | None = None,
        concurrency: int = 8,
        use_batch: bool = True,
        usage: UsageSink | None = None,
        empty_streak_limit: int = 10,
        quarantined: set[str] | None = None,
    ) -> None:
        self._mix = mix
        self._budget = budget
        self._concurrency = max(concurrency, 1)
        self._use_batch = use_batch
        self._usage = usage
        self._budget_exhausted = False   # log the cap being reached only once
        # Circuit breaker: a provider that returns this many empty completions in
        # a row is quarantined on the next run. A model that reasons its whole
        # token budget away and returns `content: ""` — deepseek and qwen have
        # both done it — otherwise burns real money on every call for a build's
        # whole duration. A single non-empty completion clears the streak, so an
        # occasional blank never trips it.
        #
        # Where a quarantined provider's share goes is the ProviderMix's decision
        # (its fallback pool): procedural by default, or a configured pool of
        # models + shares. The runner only detects the failure and reports the
        # quarantined set to `mix.route`.
        # ``quarantined`` seeds the set from a prior unit's findings (persisted in
        # the run state) so a build does not re-learn a dead provider unit by unit.
        self._empty_streak_limit = max(empty_streak_limit, 1)
        self._fail_streak: Counter[str] = Counter()   # empty completions AND errors
        self._quarantined: set[str] = set(quarantined or ())

    @property
    def quarantined(self) -> set[str]:
        """Providers quarantined so far — the seed set plus any this run added."""
        return set(self._quarantined)

    async def run(self, items: list[CatalogueItem]) -> RunReport:
        report = RunReport()
        # Route every item to its deterministic provider, then group so the
        # batch-capable slice can go out as one request.
        groups: dict[int, list[CatalogueItem]] = defaultdict(list)
        providers: list[TextProvider] = self._mix.providers
        index_of = {id(p): i for i, p in enumerate(providers)}
        quarantined = frozenset(self._quarantined)
        for item in items:
            target = self._mix.route(item_seed(item.item_id), quarantined=quarantined)
            if target is PROCEDURAL:
                # No live model for this item (its provider is quarantined and the
                # fallback pool sent its share to procedural, or is exhausted).
                report.failures[item.item_id] = "quarantined — fallback to procedural"
                continue
            groups[index_of[id(target)]].append(item)

        for idx, group in groups.items():
            provider = providers[idx]
            if self._use_batch and hasattr(provider, "complete_batch"):
                await self._run_batched(provider, group, report)
            else:
                await self._run_concurrent(provider, group, report)
        if self._usage is not None:
            self._usage.flush()
        return report

    async def _run_batched(
        self, provider: TextProvider, group: list[CatalogueItem], report: RunReport
    ) -> None:
        estimate = sum(
            (provider.estimate_batch_cost(it.request) for it in group), Decimal(0)
        )
        if self._budget is not None:
            try:
                self._budget.check(estimate)
            except BudgetExceeded as exc:
                # Over budget before sending: the whole group stays pending and
                # degrades to procedural, exactly as the concurrent path does per
                # item. Never abort the unit for it.
                if not self._budget_exhausted:
                    self._budget_exhausted = True
                    _log.info("budget reached; remaining items fall back to procedural",
                              detail=str(exc))
                for it in group:
                    report.failures[it.item_id] = repr(exc)
                return
        try:
            results = await provider.complete_batch([it.request for it in group])
        except Exception as exc:  # noqa: BLE001 - one batch failing must not abort the rest
            _log.warning("batch failed", provider=provider.name,
                         items=len(group), error=repr(exc))
            for it in group:
                report.failures[it.item_id] = repr(exc)
            # One failed batch = one failed call toward the quarantine streak.
            self._note_unusable(provider.name, provider.model, repr(exc))
            return
        for it, result in zip(group, results, strict=True):
            self._record_usage(it, result, is_batch=True)
            self._record(report, it.item_id, result)

    async def _run_concurrent(
        self, provider: TextProvider, group: list[CatalogueItem], report: RunReport
    ) -> None:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def one(item: CatalogueItem) -> CompletionResult | Exception:
            async with semaphore:
                try:
                    if self._budget is not None:
                        self._budget.check(provider.estimate_cost(item.request))
                    return await provider.complete(item.request)
                except Exception as exc:  # noqa: BLE001 - collected per item
                    return exc

        # Results are consumed **as they complete**, not after a `gather` over the
        # whole group. That is what lets the circuit breaker act mid-round: once
        # this provider is quarantined we stop waiting on its remaining calls,
        # cancel them, and let their items fall to the fallback pool next round.
        # With `gather` a provider that *hangs* (holds the connection to the
        # per-call timeout) never lets the post-loop run, so the breaker could
        # never fire and one dead provider could burn a whole task's wall clock.
        fut_item = {asyncio.ensure_future(one(it)): it for it in group}
        pending = set(fut_item)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    item, outcome = fut_item[fut], fut.result()
                    if isinstance(outcome, Exception):
                        report.failures[item.item_id] = repr(outcome)
                        # A raised call is as "unusable" as an empty answer — a bad
                        # key, a down endpoint, a hung connection — and counts
                        # toward the quarantine streak. A budget decline does not:
                        # that is the cap talking, not the provider failing.
                        if not isinstance(outcome, BudgetExceeded):
                            self._note_unusable(provider.name, provider.model, repr(outcome))
                    else:
                        self._record_usage(item, outcome)
                        self._record(report, item.item_id, outcome)
                if provider.name in self._quarantined:
                    break   # dead provider — stop paying for the rest of its share
        finally:
            for fut in pending:
                fut.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                for fut in pending:
                    report.failures[fut_item[fut].item_id] = (
                        f"provider {provider.name} quarantined mid-round — not sent")

    def _record_usage(
        self, item: CatalogueItem, result: CompletionResult, *, is_batch: bool = False
    ) -> None:
        """Attribute one call's cost to the catalogue item it generated.

        A catalogue item is reused across many documents, so per-document cost
        here is *amortised* over its users rather than measured — which is why
        this sets ``catalogue_item_id`` and leaves ``document_id`` unset. Mixing
        the two would quietly turn an amortised figure into a claimed direct one.
        """
        if self._usage is None:
            return
        metadata = {"catalogue_item_id": item.item_id, **item.request.metadata}
        self._usage.record(
            LlmUsage.from_completion(result, metadata=metadata, is_batch=is_batch)
        )

    def _record(self, report: RunReport, item_id: str, result: CompletionResult) -> None:
        # Cost and routing are recorded even when the text is unusable: the call
        # was billed, so the budget must see it. Only success/failure differs.
        report.by_provider[result.provider] += 1
        report.total_cost += result.cost
        if self._budget is not None:
            # ``add`` records the spend and *then* raises once the cap is crossed.
            # That call already happened and is billed, so the raise must not
            # abort the unit — it only means no *further* calls should go out.
            # The pre-flight ``check`` in the item paths enforces exactly that
            # (it declines once spent > cap, so remaining items stay pending and
            # degrade to the procedural description). Letting this propagate
            # instead would throw away every LLM result in the unit and fail a
            # unit the design intends to complete — a budget cap is a spend
            # ceiling on a complete build, not a reason to abandon one.
            try:
                self._budget.add(result.cost)
            except BudgetExceeded as exc:
                if not self._budget_exhausted:
                    self._budget_exhausted = True
                    _log.info("budget reached; remaining items fall back to procedural",
                              detail=str(exc))
        _log.debug("completion", item=item_id, provider=result.provider,
                   model=result.model, input_tokens=result.usage.input_tokens,
                   output_tokens=result.usage.output_tokens, cost=str(result.cost))

        if not result.text.strip():
            _log.warning("empty completion", item=item_id, provider=result.provider,
                         model=result.model, output_tokens=result.usage.output_tokens)
            # An empty completion is a failure, not a success. A reasoning model
            # that spends its whole token budget thinking returns `content: ""`
            # with a normal 200 and real usage — observed from deepseek, which
            # burned all 48 completion tokens on `reasoning_content` and emitted
            # no answer. Recording that as a result would bake blank descriptions
            # into a catalogue and report the build clean.
            report.failures[item_id] = (
                f"empty completion from {result.provider}/{result.model} "
                f"({result.usage.output_tokens} output tokens, cost {result.cost}) — "
                "if this is a reasoning model, disable thinking or raise max_tokens"
            )
            self._note_unusable(result.provider, result.model, "empty")
            return
        self._fail_streak[result.provider] = 0   # a good answer clears the streak
        report.results[item_id] = result

    def _note_unusable(self, provider: str, model: str, reason: str) -> None:
        """Count a consecutive **unusable** outcome from a provider — an empty
        completion *or* a raised call (a bad key, a down endpoint, a rate limit) —
        and quarantine it once the streak crosses the limit, so the next run
        routes its share through the fallback pool instead of paying it to fail
        again. A single usable answer clears the streak, so a transient blip never
        trips it."""
        self._fail_streak[provider] += 1
        if (self._fail_streak[provider] >= self._empty_streak_limit
                and provider not in self._quarantined):
            self._quarantined.add(provider)
            _log.warning("provider quarantined — routing its share through fallback",
                         provider=provider, model=model, reason=reason,
                         fail_streak=self._fail_streak[provider])
