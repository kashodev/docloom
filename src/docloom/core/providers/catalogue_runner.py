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

from docloom.core.providers.base import CompletionRequest, CompletionResult, TextProvider
from docloom.core.providers.budget import BudgetGuard
from docloom.core.providers.mix import ProviderMix


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
    ) -> None:
        self._mix = mix
        self._budget = budget
        self._concurrency = max(concurrency, 1)
        self._use_batch = use_batch

    async def run(self, items: list[CatalogueItem]) -> RunReport:
        report = RunReport()
        # Route every item to its deterministic provider, then group so the
        # batch-capable slice can go out as one request.
        groups: dict[int, list[CatalogueItem]] = defaultdict(list)
        providers: list[TextProvider] = self._mix.providers
        index_of = {id(p): i for i, p in enumerate(providers)}
        for item in items:
            provider = self._mix.choose(item_seed(item.item_id))
            groups[index_of[id(provider)]].append(item)

        for idx, group in groups.items():
            provider = providers[idx]
            if self._use_batch and hasattr(provider, "complete_batch"):
                await self._run_batched(provider, group, report)
            else:
                await self._run_concurrent(provider, group, report)
        return report

    async def _run_batched(
        self, provider: TextProvider, group: list[CatalogueItem], report: RunReport
    ) -> None:
        estimate = sum(
            (provider.estimate_batch_cost(it.request) for it in group), Decimal(0)
        )
        if self._budget is not None:
            self._budget.check(estimate)
        try:
            results = await provider.complete_batch([it.request for it in group])
        except Exception as exc:  # noqa: BLE001 - one batch failing must not abort the rest
            for it in group:
                report.failures[it.item_id] = repr(exc)
            return
        for it, result in zip(group, results, strict=True):
            self._record(report, it.item_id, result)

    async def _run_concurrent(
        self, provider: TextProvider, group: list[CatalogueItem], report: RunReport
    ) -> None:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def one(item: CatalogueItem) -> tuple[str, CompletionResult | Exception]:
            async with semaphore:
                try:
                    if self._budget is not None:
                        self._budget.check(provider.estimate_cost(item.request))
                    return item.item_id, await provider.complete(item.request)
                except Exception as exc:  # noqa: BLE001 - collected per item
                    return item.item_id, exc

        for item_id, outcome in await asyncio.gather(*(one(it) for it in group)):
            if isinstance(outcome, Exception):
                report.failures[item_id] = repr(outcome)
            else:
                self._record(report, item_id, outcome)

    def _record(self, report: RunReport, item_id: str, result: CompletionResult) -> None:
        report.results[item_id] = result
        report.by_provider[result.provider] += 1
        report.total_cost += result.cost
        if self._budget is not None:
            self._budget.add(result.cost)
