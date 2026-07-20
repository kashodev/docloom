"""Pluggable LLM text providers for catalogue generation.

One protocol (:class:`TextProvider`), several adapters, and a weighted
:class:`ProviderMix` — configured entirely in the run config's ``providers``
block. DeepSeek and Qwen ride one OpenAI-compatible adapter; Claude uses its own
SDK; Ollama runs local models for free. A :class:`BudgetGuard` enforces a hard
dollar ceiling.

Only used for the one-time, offline catalogue step — document generation reads
the finished catalogue and makes no LLM calls.
"""

from docloom.core.providers.base import (
    CompletionRequest,
    CompletionResult,
    TextProvider,
    Usage,
)
from docloom.core.providers.budget import BudgetExceeded, BudgetGuard
from docloom.core.providers.factory import PRESETS, build_mix, build_provider
from docloom.core.providers.mix import ProviderMix
from docloom.core.providers.openai_compatible import OpenAICompatibleProvider
from docloom.core.providers.pricing import PRICING, Pricing, pricing_for

__all__ = [
    "PRESETS",
    "PRICING",
    "BudgetExceeded",
    "BudgetGuard",
    "CompletionRequest",
    "CompletionResult",
    "OpenAICompatibleProvider",
    "Pricing",
    "ProviderMix",
    "TextProvider",
    "Usage",
    "build_mix",
    "build_provider",
    "pricing_for",
]
