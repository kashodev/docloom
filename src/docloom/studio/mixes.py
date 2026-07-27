"""Named LLM provider mixes for the catalogue build.

A *mix* is a small, known-good preset: which providers write the product
descriptions, in what proportion, which Secret Manager keys they need, and how a
quarantined provider's share is redistributed. It expands to the exact
``catalogue.providers`` / ``fallback`` / ``secrets`` block that ``deploy.sh`` and
``docloom catalogue --providers`` already understand (see
``deploy/gcp/run.example.yaml``), so the studio only ever picks a preset — it
never hand-authors the block, and it never touches a key *value*.

``procedural`` is the key-free default: no providers, so the build stays the
combinatorial pool with no spend — exactly today's behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

from docloom.studio.types import StudioError


@dataclass(frozen=True, slots=True)
class Mix:
    """One provider-mix preset. ``providers`` empty ⇒ the procedural build."""

    name: str
    description: str
    providers: tuple[dict, ...] = ()          # each: {name, model, weight, [extra_body]}
    fallback: tuple[dict, ...] = ()           # each: {name, share}; quarantine redistribution
    secrets: tuple[tuple[str, str], ...] = ()  # (env var, default Secret Manager name)

    @property
    def is_llm(self) -> bool:
        return bool(self.providers)

    def providers_block(self) -> list[dict]:
        return [dict(p) for p in self.providers]

    def fallback_block(self) -> list[dict]:
        return [dict(f) for f in self.fallback]

    def secrets_map(self) -> dict[str, str]:
        """``env var → secret name`` — the ``catalogue.secrets`` block. Names only."""
        return {env: name for env, name in self.secrets}


PROCEDURAL = Mix("procedural", "no LLM — the combinatorial pool, no keys, no spend")

# The cheapest LLM mix: two low-cost OpenAI-compatible models only, no Anthropic.
# Thinking is disabled on Qwen (it otherwise ignores max_tokens), and a quarantined
# provider's share leans on the cheap survivor then procedural.
CHEAP = Mix(
    "cheap-mix",
    "deepseek 50 / dashscope 50 — cheap OpenAI-compatible only (thinking off on qwen)",
    providers=(
        # Both are reasoning models: without thinking disabled they burn the whole
        # token budget on reasoning_content and return empty text. deepseek and
        # dashscope disable it with different vendor params.
        {"name": "deepseek", "model": "deepseek-v4-flash", "weight": 50,
         "extra_body": {"thinking": {"type": "disabled"}}},
        {"name": "dashscope", "model": "qwen3.5-flash", "weight": 50,
         "extra_body": {"enable_thinking": False}},
    ),
    fallback=({"name": "dashscope", "share": 70}, {"name": "procedural", "share": 30}),
    secrets=(("DEEPSEEK_API_KEY", "deepseek-api-key"),
             ("DASHSCOPE_API_KEY", "dashscope-api-key")),
)

# The reference mix from deploy/gcp/run.example.yaml: the two cheap models plus a
# 20% Anthropic slice for a quality floor on the semantic long tail.
BALANCED = Mix(
    "balanced",
    "deepseek 40 / dashscope 40 / anthropic 20 — a 20% Anthropic slice for quality",
    providers=(
        {"name": "deepseek", "model": "deepseek-v4-flash", "weight": 40,
         "extra_body": {"thinking": {"type": "disabled"}}},   # reasoning off (see cheap-mix)
        {"name": "dashscope", "model": "qwen3.5-flash", "weight": 40,
         "extra_body": {"enable_thinking": False}},
        {"name": "anthropic", "model": "claude-haiku-4-5", "weight": 20},
    ),
    fallback=({"name": "dashscope", "share": 70}, {"name": "procedural", "share": 30}),
    secrets=(("DEEPSEEK_API_KEY", "deepseek-api-key"),
             ("DASHSCOPE_API_KEY", "dashscope-api-key"),
             ("ANTHROPIC_API_KEY", "anthropic-api-key")),
)

ANTHROPIC = Mix(
    "anthropic",
    "anthropic only — claude-haiku-4-5 writes every description",
    providers=({"name": "anthropic", "model": "claude-haiku-4-5", "weight": 100},),
    fallback=({"name": "procedural", "share": 100},),
    secrets=(("ANTHROPIC_API_KEY", "anthropic-api-key"),),
)

_MIXES: dict[str, Mix] = {m.name: m for m in (PROCEDURAL, CHEAP, BALANCED, ANTHROPIC)}


def mix_names() -> tuple[str, ...]:
    return tuple(_MIXES)


def get_mix(name: str) -> Mix:
    mix = _MIXES.get(name)
    if mix is None:
        raise StudioError(f"unknown provider mix {name!r}; available: {', '.join(_MIXES)}")
    return mix
