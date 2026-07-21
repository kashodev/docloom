"""Archetype coverage tests.

Two genuinely distinct skeletons beyond the flat and telecom archetypes — the
compact receipt and the framed form — plus the variation matrix that collapses
the rest of the 39-slug corpus into modifier classes on the flat archetype. The
point is structural diversity an extractor can't learn from one skeleton.
"""

from __future__ import annotations

from collections import Counter

import docloom.packs  # noqa: F401
from docloom.core import get_pack
from docloom.core.render import render_record
from docloom.packs.invoice import InvoiceSampler, SeedCatalogue
from tests.factories import invoice, simple_lines, profile

PACK = get_pack("invoice")


def render_with(archetype: str) -> str:
    inv = invoice(simple_lines(), render_profile=profile(archetype=archetype))
    return render_record(PACK, inv)


# ── the two new skeletons render ────────────────────────────────────────────
def test_receipt_archetype_is_a_compact_slip() -> None:
    out = render_with("receipt-compact-01")
    assert out.startswith("<!doctype html>")
    assert 'class="receipt"' in out
    assert "Northwind Supply" in out
    assert "$105.00" in out


def test_boxed_form_archetype_is_framed() -> None:
    out = render_with("boxed-form-01")
    assert 'class="form"' in out
    assert 'class="form-meta"' in out          # labelled bordered meta cells
    assert "INV-2026-0042" in out
    assert "Acme Industrial" in out            # recipient in a bordered box


# ── variation matrix: modifiers, not new templates ──────────────────────────
def test_top_row_meta_gets_the_banner_modifier() -> None:
    from docloom.packs.invoice import body_classes
    inv = invoice(simple_lines(), render_profile=profile(meta_position="top-row"))
    assert "meta-banner" in body_classes(inv)
    assert "meta-banner" in render_record(PACK, inv)


# ── archetype variety across the roster & sampled invoices ──────────────────
def test_roster_spreads_across_archetypes() -> None:
    arch = Counter(c.render_profile.archetype for c in SeedCatalogue().roster().companies)
    assert set(arch) == {
        "meta-sidebar-01", "boxed-form-01", "receipt-compact-01", "telecom-itemized-37",
    }
    assert arch["meta-sidebar-01"] > arch["receipt-compact-01"]   # flat is the majority


def test_all_archetypes_appear_and_render_in_sampled_invoices() -> None:
    sampler = InvoiceSampler(max_line_items=80)
    seen: set[str] = set()
    for i in range(4000):
        inv = sampler.generate("rv", i)
        a = inv.render_profile.archetype
        if a not in seen:
            assert render_record(PACK, inv).startswith("<!doctype html>")
            seen.add(a)
        if len(seen) == 4:
            break
    assert seen == {
        "meta-sidebar-01", "boxed-form-01", "receipt-compact-01", "telecom-itemized-37",
    }


# ── telecom archetype renders its hierarchy from generated data ─────────────
def test_telecom_invoices_are_grouped_and_render_the_hierarchy() -> None:
    sampler = InvoiceSampler(max_line_items=60)
    telecom = next(
        inv for i in range(500)
        if (inv := sampler.generate("tel", i)).business_type.value == "telecom"
    )
    # Grouping was applied: lines carry subscriber group keys and sections.
    assert any(li.group_key for li in telecom.line_items)
    assert any(li.section for li in telecom.line_items)
    out = render_record(PACK, telecom)
    assert "555-" in out                       # subscriber numbers as group heads
    assert telecom.totals.grand_total > 0      # and it still reconciles (it built)
