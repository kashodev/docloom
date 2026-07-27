#!/usr/bin/env python3
"""Smoke test the LLM catalogue path against the *real* three providers.

This drives `CatalogueRunner` — the exact machinery a real catalogue build would
use — over a handful of tiny synthetic items, under a hard budget cap. It proves
the things you want proven before spending real money at scale:

  * each API key works and the endpoint responds;
  * deterministic routing actually spreads items across the 40/40/20 mix;
  * cost is computed from real token usage;
  * the budget guard trips before an overspend.

It is NOT the deploy path (there is no `docsynth catalogue` command yet) — it
calls the runner directly, which is the smallest honest test of the provider
layer. Keys come from the environment; nothing is written anywhere.

Needs the anthropic extra for the Claude slice:  pip install 'docsynth[anthropic]'

    export ANTHROPIC_API_KEY=... DEEPSEEK_API_KEY=... DASHSCOPE_API_KEY=...
    python scripts/smoke_catalogue.py                 # ~9 items, $0.50 cap, synchronous
    python scripts/smoke_catalogue.py --batch         # exercise the Anthropic batch path too
    python scripts/smoke_catalogue.py --dry-run       # build + route, make NO calls
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docsynth.core.providers.base import CompletionRequest
from docsynth.core.providers.budget import BudgetExceeded, BudgetGuard
from docsynth.core.providers.catalogue_runner import CatalogueItem, CatalogueRunner
from docsynth.core.providers.factory import build_provider
from docsynth.core.providers.mix import ProviderMix

# The same mix as deploy/gcp/run.yaml. Model ids are the config's — a wrong id is
# exactly the kind of thing this smoke should surface, as a per-item failure.
SPECS = [
    {"name": "deepseek", "model": "deepseek-v4-flash", "weight": 40},
    {"name": "dashscope", "model": "qwen3.5-flash", "weight": 40},
    {"name": "anthropic", "model": "claude-haiku-4-5", "weight": 20},
]

# What each provider reads for its key (factory.py). Anthropic via its own SDK.
KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "dashscope": "DASHSCOPE_API_KEY",
           "anthropic": "ANTHROPIC_API_KEY"}

_PRODUCTS = [
    "stainless steel hex bolt, M8", "LED work light, 20W", "thermal receipt paper",
    "corrugated shipping carton", "industrial pallet wrap", "nitrile gloves, box of 100",
    "cordless impact driver", "safety goggles, anti-fog", "duct tape, 48mm",
    "cable ties, 200mm", "shop rag bundle", "padlock, brass 40mm",
]


def make_items(n: int) -> list[CatalogueItem]:
    """Tiny asks — small prompts, small max_tokens — so the whole run costs cents."""
    items = []
    for i in range(n):
        product = _PRODUCTS[i % len(_PRODUCTS)]
        items.append(CatalogueItem(
            item_id=f"prod-{i:03d}",
            request=CompletionRequest(
                system="You write invoice line-item labels: a terse noun phrase with a spec, like a catalogue entry — never a sentence, never marketing.",
                prompt=f"Write one invoice line item for: {product}. Reply with the label only, e.g. 'Stainless steel hex bolt, M8 x 40mm (pack of 10)'.",
                max_tokens=48,
                metadata={"product": product},
            ),
        ))
    return items


def check_keys(dry_run: bool) -> None:
    missing = [env for name, env in KEY_ENV.items() if not os.environ.get(env)]
    if missing and not dry_run:
        print(f"✗ missing key(s) in the environment: {', '.join(missing)}")
        print("  export them and re-run, e.g.  export DEEPSEEK_API_KEY=sk-...")
        raise SystemExit(2)
    for name, env in KEY_ENV.items():
        mark = "set" if os.environ.get(env) else "MISSING"
        print(f"  {name:10s} {env:20s} {mark}")


async def dump_raw() -> None:
    """Post one request to each OpenAI-compatible endpoint and print the raw
    message shape — the definitive check for reasoning-model behaviour. Bypasses
    the provider's parsing entirely so we see exactly what the API returned."""
    import json

    import httpx

    from docsynth.core.providers.factory import PRESETS

    req = CompletionRequest(
        system="You write invoice line-item labels: a terse noun phrase with a spec, never a sentence.",
        prompt="Write one invoice line item for: LED work light, 20W. Label only.",
        max_tokens=48,
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        for spec in SPECS:
            name, model = spec["name"], spec["model"]
            if name == "anthropic":
                continue  # separate SDK; it behaved correctly
            preset = PRESETS[name]
            base = os.environ.get(preset.base_url_env, preset.default_base_url)
            key = os.environ.get(preset.key_env, "")
            body = {"model": model, "max_tokens": req.max_tokens,
                    "messages": [{"role": "system", "content": req.system},
                                 {"role": "user", "content": req.prompt}]}
            print(f"\n── {name} / {model} ──────────────────────────────")
            try:
                r = await client.post(f"{base.rstrip('/')}/chat/completions",
                                      json=body, headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                data = r.json()
                msg = data["choices"][0]["message"]
                usage = data.get("usage", {})
                content = msg.get("content")
                reasoning = msg.get("reasoning_content") or msg.get("reasoning")
                print(f"  message keys:      {sorted(msg)}")
                print(f"  content:           {content!r}"[:90])
                print(f"  content is empty:  {not content}")
                print(f"  reasoning_content: {'present, ' + str(len(reasoning)) + ' chars' if reasoning else 'absent'}")
                if reasoning:
                    print(f"    reasoning head:  {reasoning[:80]!r}")
                print(f"  finish_reason:     {data['choices'][0].get('finish_reason')}")
                print(f"  usage:             {json.dumps(usage)}")
            except Exception as exc:
                print(f"  request failed: {exc!r}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=9)
    ap.add_argument("--budget", type=str, default="0.50", help="hard USD cap")
    ap.add_argument("--batch", action="store_true",
                    help="use the Anthropic Message Batches path (polls; slower)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build providers and route items, but make no API calls")
    ap.add_argument("--raw", action="store_true",
                    help="dump the raw message object from each OpenAI-compatible "
                         "provider (proves whether reasoning_content is eating the "
                         "token budget), then exit")
    args = ap.parse_args()

    print("── keys ───────────────────────────────────────────────")
    check_keys(args.dry_run)

    providers = [build_provider(s) for s in SPECS]
    weights = [float(s["weight"]) for s in SPECS]
    mix = ProviderMix(providers, weights)
    items = make_items(args.items)

    # Show the deterministic routing plan first — proves the mix spreads work
    # without spending anything.
    print("\n── routing plan (deterministic per item id) ───────────")
    from docsynth.core.providers.catalogue_runner import item_seed
    plan: dict[str, int] = {}
    for it in items:
        p = mix.choose(item_seed(it.item_id))
        plan[p.name] = plan.get(p.name, 0) + 1
    for name in (s["name"] for s in SPECS):
        print(f"  {name:10s} {plan.get(name, 0)} item(s)")

    if args.dry_run:
        print("\n✓ dry run: wiring OK, no calls made")
        return 0

    if args.raw:
        await dump_raw()
        return 0

    budget = BudgetGuard(Decimal(args.budget))
    runner = CatalogueRunner(mix, budget=budget, concurrency=4, use_batch=args.batch)

    print(f"\n── running {len(items)} item(s), budget ${args.budget}, "
          f"batch={'on' if args.batch else 'off'} ──")
    try:
        report = await runner.run(items)
    except BudgetExceeded as exc:
        print(f"\n✗ budget tripped (as designed): {exc}")
        return 1

    print("\n── results ────────────────────────────────────────────")
    for item_id in sorted(report.results):
        r = report.results[item_id]
        text = " ".join(r.text.split())
        print(f"  {item_id}  {r.provider:10s} {r.model:18s} "
              f"${r.cost:.6f}  in={r.usage.input_tokens} out={r.usage.output_tokens}")
        print(f"            “{text}”")
    for item_id in sorted(report.failures):
        print(f"  {item_id}  FAILED: {report.failures[item_id][:120]}")

    print("\n── summary ────────────────────────────────────────────")
    print(f"  routed:   {dict(report.by_provider)}")
    print(f"  ok:       {len(report.results)}   failed: {len(report.failures)}")
    print(f"  total:    ${report.total_cost:.6f}  of ${args.budget} cap")
    if report.failures:
        print("\n✗ some items failed — a bad key, model id, or endpoint. See above.")
        return 1
    print("\n✓ all providers responded and were costed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
