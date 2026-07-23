"""Build providers and a mix from config.

Turns the ``providers.text`` block of a run config into a :class:`ProviderMix`.
Each spec names a provider preset, a model, and a weight; the preset knows the
endpoint and which env var holds the key:

    providers:
      text:
        - {name: deepseek,  model: deepseek-v4-flash, weight: 0.4}
        - {name: dashscope, model: qwen3.5-flash,     weight: 0.4}
        - {name: anthropic, model: claude-haiku-4-5,  weight: 0.2}
      budget: {limit_usd: 50, abort_on_exceed: true}

Keys are read from the environment at build time, never stored in the config —
the config is committed to the run store, so a key in it would leak.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from docloom.core.providers.base import TextProvider
from docloom.core.providers.budget import BudgetGuard
from docloom.core.providers.mix import ProviderMix
from docloom.core.providers.openai_compatible import OpenAICompatibleProvider


@dataclass(frozen=True, slots=True)
class _Preset:
    default_base_url: str
    base_url_env: str | None
    key_env: str | None


# OpenAI-compatible presets. Anthropic is handled separately (own SDK).
PRESETS: dict[str, _Preset] = {
    "deepseek": _Preset("https://api.deepseek.com", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    "dashscope": _Preset(
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_BASE_URL",
        "DASHSCOPE_API_KEY",
    ),
    # Local Ollama — no key, free pricing (model "__local__").
    "ollama": _Preset("http://localhost:11434/v1", "OLLAMA_BASE_URL", None),
}


def build_provider(spec: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> TextProvider:
    """Construct one provider from a spec dict."""
    name = spec["name"]
    model = spec["model"]

    if name == "anthropic":
        # Imported here so the factory does not require the anthropic extra
        # unless an anthropic provider is actually configured.
        from docloom.core.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model, api_key=os.environ.get("ANTHROPIC_API_KEY"))

    preset = PRESETS.get(name)
    if preset is None:
        raise ValueError(f"unknown provider {name!r} (known: {', '.join(sorted(PRESETS))}, anthropic)")

    base_url = spec.get("base_url") or (
        os.environ.get(preset.base_url_env, preset.default_base_url)
        if preset.base_url_env
        else preset.default_base_url
    )
    api_key = os.environ.get(preset.key_env) if preset.key_env else None
    return OpenAICompatibleProvider(
        name=name, model=model, base_url=base_url, api_key=api_key, client=client,
        # Provider-specific request parameters, straight from config — e.g.
        # `extra_body: {enable_thinking: false}` to stop a Qwen model reasoning.
        extra_body=spec.get("extra_body"),
    )


def build_mix(config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> ProviderMix:
    """Construct a :class:`ProviderMix` from a ``providers`` config block."""
    specs = config["text"]
    providers = [build_provider(spec, client=client) for spec in specs]
    weights = [float(spec.get("weight", 1.0)) for spec in specs]

    budget = None
    if "budget" in config:
        b = config["budget"]
        budget = BudgetGuard(
            Decimal(str(b["limit_usd"])), abort_on_exceed=b.get("abort_on_exceed", True)
        )
    return ProviderMix(providers, weights, budget=budget)
