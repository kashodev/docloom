"""Pluggable LLM text providers for catalogue generation.

One protocol (:class:`TextProvider`), several adapters, and a weighted
:class:`ProviderMix` — configured entirely in the run config's ``providers``
block. DeepSeek and Qwen ride one OpenAI-compatible adapter; Claude uses its own
SDK; Ollama runs local models for free. A :class:`BudgetGuard` enforces a hard
dollar ceiling.

Only used for the one-time, offline catalogue step — document generation reads
the finished catalogue and makes no LLM calls.
"""

from docsynth.core.providers.base import (
    CompletionRequest,
    CompletionResult,
    TextProvider,
    Usage,
)
from docsynth.core.providers.budget import BudgetExceeded, BudgetGuard
from docsynth.core.providers.catalogue_runner import (
    CatalogueItem,
    CatalogueRunner,
    RunReport,
    item_seed,
)
from docsynth.core.providers.factory import PRESETS, build_mix, build_provider
from docsynth.core.providers.mix import ProviderMix
from docsynth.core.providers.openai_compatible import OpenAICompatibleProvider
from docsynth.core.providers.pricing import PRICING, Pricing, pricing_for

__all__ = [
    "PRESETS",
    "PRICING",
    "BudgetExceeded",
    "BudgetGuard",
    "CatalogueItem",
    "CatalogueRunner",
    "CompletionRequest",
    "CompletionResult",
    "OpenAICompatibleProvider",
    "Pricing",
    "ProviderMix",
    "RunReport",
    "TextProvider",
    "Usage",
    "build_mix",
    "build_provider",
    "item_seed",
    "pricing_for",
]
