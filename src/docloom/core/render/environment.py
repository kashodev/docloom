"""Jinja environment — the render kernel.

Document-type agnostic. It knows how to format money, dates, rates and
quantities for a locale, and how to look a label up in whatever registry the
active pack supplied. It knows nothing about invoices, contracts, or any other
document shape.

Formatting filters are *context-aware*: they read ``locale``, ``currency`` and
``labels`` from the render context rather than taking them as arguments. That
keeps templates readable — ``{{ line.amount | money }}`` rather than
``{{ line.amount | money(locale, currency) }}`` repeated hundreds of times — and
makes it impossible for a template to format a value in the wrong locale by
forgetting an argument.

Autoescaping is on. Generated text is untrusted as far as the renderer is
concerned; a stray ``<`` in an LLM-written description must not be able to break
the document structure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context
from jinja2.runtime import Context

from docloom.core.pack import DocumentPack
from docloom.core.record import GoldenRecord
from docloom.core.locale.formatting import (
    format_amount,
    format_date,
    format_date_long,
    format_quantity,
    format_rate,
)


@pass_context
def f_money(ctx: Context, value: Decimal | None, with_symbol: bool = True) -> str:
    if value is None:
        return ""
    return format_amount(value, ctx["currency"], ctx["locale"], with_symbol=with_symbol)


@pass_context
def f_date(ctx: Context, value: date | None) -> str:
    return "" if value is None else format_date(value, ctx["locale"])


@pass_context
def f_date_long(ctx: Context, value: date | None) -> str:
    return "" if value is None else format_date_long(value, ctx["locale"])


@pass_context
def f_rate(ctx: Context, value: Decimal | None) -> str:
    return "" if value is None else format_rate(value, ctx["locale"])


@pass_context
def f_qty(ctx: Context, value: Decimal | None) -> str:
    return "" if value is None else format_quantity(value, ctx["locale"])


@pass_context
def f_label(ctx: Context, key: str) -> str:
    """Look up a printed label in the active pack's registry.

    Raises on an unknown key rather than rendering blank — a missing
    translation must fail at generation time, not appear as an empty heading on
    every document in the run.
    """
    return ctx["labels"].get(ctx["language"], key)


def build_environment(template_root: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(template_root),
        autoescape=True,
        undefined=StrictUndefined,   # a typo'd variable fails loudly, not silently
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(
        {
            "money": f_money,
            "date": f_date,
            "date_long": f_date_long,
            "rate": f_rate,
            "qty": f_qty,
            "label": f_label,
        }
    )
    return env


def render_template(template_root: Path, archetype: str, context: dict[str, Any]) -> str:
    """Render one archetype to an HTML string."""
    env = build_environment(template_root)
    return env.get_template(f"archetypes/{archetype}.html.j2").render(**context)


def render_record(pack: "DocumentPack", record: "GoldenRecord") -> str:
    """Render one record using its pack.

    The normal entry point: the pack chooses the archetype and builds the
    context, the kernel does the weaving. Callers never touch template paths.
    """
    return render_template(pack.template_root, pack.archetype_for(record), pack.build_context(record))
