"""LLM usage telemetry — the row, and the sink protocol.

Every LLM call is recorded: which model, how many tokens, what it cost, and which
document or catalogue item it was for. That makes "what did this one PDF cost, on
which model?" a query rather than a guess.

Two decisions shape everything here.

**This is not golden data.** Token counts are not reproducible — regenerate a
document and they differ. The golden record's entire value is that it is exactly
recomputable, so non-deterministic observability lives in its own table with its
own contract. It is written alongside the golden shards, never inside them.

**Cost stays an unquantised ``Decimal``,** matching the discipline in
:mod:`docloom.core.providers.base`: one completion can cost a tiny fraction of a
cent, and rounding each to 2dp would sum to zero. Floats would silently lose the
long tail across a million calls.

Sinks buffer and write in batches — the caller records per call and flushes at a
natural boundary (the work unit), so the LLM call path is never blocked on I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

#: The table/collection these rows land in, across every backend.
TABLE = "llm_usage"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """One LLM call's tokens and cost, attributed to what it was generating."""

    run_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    #: Which unit produced it — the shard boundary, and the join back to the run.
    unit_index: int | None = None
    #: Set when the call generated content for one document (per-document mode).
    document_id: str | None = None
    #: Set when the call generated a *catalogue* item reused across many
    #: documents. Cost per document is then amortised over its users, not
    #: measured directly — a genuinely different number, so it gets its own
    #: column rather than being silently mixed into ``document_id``.
    catalogue_item_id: str | None = None
    pack: str | None = None
    #: Which generation step spent the money — "clauses", "recitals", …
    purpose: str | None = None
    call_index: int = 0
    cached_input_tokens: int = 0
    #: The Batch API is half price; without this the rates look wrong.
    is_batch: bool = False
    latency_ms: int | None = None
    attempt: int = 1
    #: A failed call still costs money, so failures are recorded, not dropped.
    status: str = "ok"
    error: str | None = None
    started_at: datetime = field(default_factory=_now)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_completion(
        cls,
        result: Any,
        *,
        metadata: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        is_batch: bool = False,
        attempt: int = 1,
    ) -> LlmUsage:
        """Build a row from a ``CompletionResult`` plus the request's metadata.

        Correlation ids are read from ``CompletionRequest.metadata``, which exists
        for exactly this ("carries application context … so a caller can correlate
        results"). Callers stamp ``run_id`` / ``document_id`` / ``unit_index``
        there; nothing in the provider layer has to learn about documents.
        """
        meta = metadata or {}
        usage = result.usage
        return cls(
            run_id=str(meta.get("run_id", "")),
            provider=result.provider,
            model=result.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=getattr(usage, "cached_input_tokens", 0) or 0,
            cost_usd=result.cost,
            unit_index=meta.get("unit_index"),
            document_id=meta.get("document_id"),
            catalogue_item_id=meta.get("catalogue_item_id") or meta.get("item_id"),
            pack=meta.get("pack"),
            purpose=meta.get("purpose"),
            call_index=int(meta.get("call_index", 0)),
            is_batch=is_batch,
            latency_ms=latency_ms,
            attempt=attempt,
        )

    @classmethod
    def failure(
        cls,
        *,
        provider: str,
        model: str,
        error: str,
        metadata: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> LlmUsage:
        """A call that failed. Recorded because a failed call is often still
        billed, and because a run whose cost data omits its failures understates
        what it actually spent."""
        meta = metadata or {}
        return cls(
            run_id=str(meta.get("run_id", "")),
            provider=provider,
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
            unit_index=meta.get("unit_index"),
            document_id=meta.get("document_id"),
            catalogue_item_id=meta.get("catalogue_item_id") or meta.get("item_id"),
            pack=meta.get("pack"),
            purpose=meta.get("purpose"),
            call_index=int(meta.get("call_index", 0)),
            attempt=attempt,
            status="error",
            error=error[:500],
        )

    def to_row(self) -> dict[str, Any]:
        """Flatten for a columnar table. ``cost_usd`` stays ``Decimal``; the
        golden codec already round-trips it exactly."""
        return {
            "run_id": self.run_id,
            "unit_index": self.unit_index,
            "document_id": self.document_id,
            "catalogue_item_id": self.catalogue_item_id,
            "pack": self.pack,
            "purpose": self.purpose,
            "call_index": self.call_index,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "is_batch": self.is_batch,
            "latency_ms": self.latency_ms,
            "attempt": self.attempt,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
        }


@runtime_checkable
class UsageSink(Protocol):
    """Somewhere LLM usage rows land.

    ``record`` buffers, ``flush`` persists. Splitting them keeps the LLM call
    path off the I/O path: a worker records per call and flushes once per unit.
    """

    scheme: str

    def record(self, usage: LlmUsage) -> None:
        """Buffer one call's usage."""
        ...

    def flush(self) -> int:
        """Persist everything buffered. Returns how many rows were written."""
        ...

    def close(self) -> None:
        """Flush and release resources."""
        ...


class NullUsageSink:
    """Records nothing — telemetry explicitly disabled.

    Exists so "off" is a sink like any other rather than a ``None`` check at
    every call site.
    """

    scheme = "null"

    def record(self, usage: LlmUsage) -> None:
        return None

    def flush(self) -> int:
        return 0

    def close(self) -> None:
        return None


class MemoryUsageSink:
    """Keeps rows in memory. For tests, and for reading totals back in-process."""

    scheme = "memory"

    def __init__(self) -> None:
        self.rows: list[LlmUsage] = []
        self.flushes = 0

    def record(self, usage: LlmUsage) -> None:
        self.rows.append(usage)

    def flush(self) -> int:
        self.flushes += 1
        return len(self.rows)

    def close(self) -> None:
        self.flush()

    @property
    def total_cost(self) -> Decimal:
        return sum((r.cost_usd for r in self.rows), Decimal(0))
