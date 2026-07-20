"""Text-generation providers — the pluggable LLM layer.

docloom generates document *content* (catalogue product descriptions, company
identities, boilerplate) once, offline, with an LLM. The document run itself
makes no LLM calls — it reads the pre-built catalogue. So this layer is about
the catalogue step, where the value of pluggability is a configurable *mix* of
models: cheap Chinese models for the bulk, a pricier one for lexical diversity,
a local model for zero-cost dev.

Every provider answers the same two questions — "complete this prompt" and "what
would that cost" — so a run can be planned against a budget before a token is
spent, and the mix can be swapped in config without touching calling code.

Costs are raw ``Decimal``, never quantised to cents: a single completion can
cost a small fraction of a cent, and rounding each to 2dp would sum to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from docloom.core.providers.pricing import Pricing


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting from one completion."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0   # prompt tokens served from the provider's cache


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """A single generation ask.

    ``metadata`` carries application context (e.g. the catalogue item id) so a
    caller can correlate results; providers ignore it.
    """

    system: str
    prompt: str
    max_tokens: int = 1024
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """The text plus everything needed to account for it."""

    text: str
    usage: Usage
    model: str
    provider: str
    cost: Decimal


@runtime_checkable
class TextProvider(Protocol):
    """One model behind one endpoint."""

    name: str          # provider label, e.g. "deepseek", "anthropic", "ollama"
    model: str         # model id, e.g. "deepseek-v4-flash"
    pricing: Pricing

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Generate a completion and report exact cost from real usage."""
        ...

    def estimate_cost(self, request: CompletionRequest) -> Decimal:
        """Pre-flight cost estimate, for budgeting before the call is made.

        Deliberately approximate — a token heuristic on the prompt plus the
        requested ``max_tokens`` — because its only job is to keep a run from
        starting work it cannot afford. Actual cost comes from ``complete``.
        """
        ...
