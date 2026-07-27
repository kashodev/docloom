"""Archetype coverage tests.

Two genuinely distinct skeletons beyond the flat and telecom archetypes — the
compact receipt and the framed form — plus the variation matrix that collapses
the rest of the 39-slug corpus into modifier classes on the flat archetype. The
point is structural diversity an extractor can't learn from one skeleton.
"""

from __future__ import annotations

import re
from collections import Counter

import docsynth.packs  # noqa: F401
from docsynth.core import get_pack
from docsynth.core.render import render_record
from docsynth.packs.invoice import InvoiceSampler, SeedCatalogue
from tests.factories import invoice, profile, simple_lines

PACK = get_pack("invoice")


def render_with(archetype: str) -> str:
    inv = invoice(simple_lines(), render_profile=profile(archetype=archetype))
    return render_record(PACK, inv)


def _body_only(html: str) -> str:
    """Strip the running-header <template>, whose company name is the intended
    per-page header, not a visible body duplicate."""
    return re.sub(r'<template id="running-header">.*?</template>', "", html, flags=re.S)


# ── layout: one company name, all regions placed, no phantom rail ───────────
def test_company_name_appears_once_in_the_body() -> None:
    for archetype in ("meta-sidebar-01", "receipt-compact-01", "boxed-form-01"):
        for has_logo in (True, False):
            inv = invoice(simple_lines(),
                          render_profile=profile(archetype=archetype, has_logo=has_logo))
            body = _body_only(render_record(PACK, inv))
            assert body.count("Northwind Supply") == 1, (archetype, has_logo)


def test_every_meta_position_places_all_three_regions() -> None:
    """The rail layouts used to leave the issuer block unplaced (phantom column);
    every position must now place brand, meta, and body."""
    for pos in ("top-left", "top-right", "top-row", "left-rail", "right-rail", "split"):
        html = render_record(PACK, invoice(simple_lines(),
                                           render_profile=profile(meta_position=pos)))
        assert 'class="brand"' in html
        assert 'class="doc-meta"' in html
        assert 'class="doc-body"' in html


def test_rail_layouts_use_a_single_narrow_rail() -> None:
    html = render_with("meta-sidebar-01")
    # One rail definition per side, table column dominant (1fr), rail narrow.
    assert 'grid-template-columns: 44mm 1fr' in html   # left-rail
    assert 'grid-template-columns: 1fr 44mm' in html   # right-rail


# ── fonts: distinct stacks, not escaped, size axis ──────────────────────────
def test_font_stack_is_not_html_escaped() -> None:
    """Regression: autoescape turned the quotes in the font stack into &#39;,
    producing invalid CSS so every invoice fell back to one default font."""
    html = render_record(PACK, invoice(simple_lines(),
                                       render_profile=profile(typeface="serif-classic")))
    # The bundled family leads, then the semantic fallback chain — quotes intact.
    assert "--font-body: 'DL Noto Serif', Georgia, 'Times New Roman'" in html
    assert "&#39;" not in html


def test_typefaces_resolve_to_distinct_stacks() -> None:
    from docsynth.core.fonts import font_stack
    serif = render_record(PACK, invoice(simple_lines(),
                                        render_profile=profile(typeface="serif-classic")))
    mono = render_record(PACK, invoice(simple_lines(),
                                       render_profile=profile(typeface="mono-invoice")))
    assert font_stack("serif-classic") != font_stack("mono-invoice")
    assert "Georgia" in serif and "Courier New" in mono


def test_font_scale_varies_body_size() -> None:
    html = render_record(PACK, invoice(simple_lines(),
                                       render_profile=profile(font_scale=1.08)))
    assert "--font-scale: 1.08" in html
    assert "calc(10pt * var(--font-scale))" in html


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


def test_banner_archetype_carries_colour_into_the_header() -> None:
    out = render_with("banner-header-06")
    assert 'class="banner"' in out
    # The band is filled with the company accent and reverses text to white.
    assert ".banner { background: var(--accent); color: #fff;" in out
    assert "Northwind Supply" in out
    assert "$105.00" in out
    # The company name still appears exactly once in the visible body.
    assert _body_only(out).count("Northwind Supply") == 1


def test_fullbleed_archetype_tints_the_whole_sheet() -> None:
    out = render_with("fullbleed-05")
    assert "archetype-fullbleed" in out
    assert "body.archetype-fullbleed { background: color-mix(" in out
    assert "INV-2026-0042" in out
    assert _body_only(out).count("Northwind Supply") == 1


# ── brand marks & watermark: one shared macro, per-archetype placement ──────
def test_logo_style_mark_renders_an_svg_beside_the_wordmark() -> None:
    inv = invoice(simple_lines(), render_profile=profile(logo_style="mark", has_logo=True))
    out = render_record(PACK, inv)
    assert '<svg class="logo-mark"' in out
    assert "Northwind Supply" in out            # the wordmark still shows alongside


def test_logo_style_wordmark_has_no_svg_mark() -> None:
    inv = invoice(simple_lines(), render_profile=profile(logo_style="wordmark", has_logo=True))
    out = render_record(PACK, inv)
    assert '<svg class="logo-mark"' not in out
    assert "Northwind Supply" in out


def test_no_mark_when_the_company_is_text_only() -> None:
    inv = invoice(simple_lines(), render_profile=profile(logo_style="mark", has_logo=False))
    assert '<svg class="logo-mark"' not in render_record(PACK, inv)


def test_watermark_renders_only_when_enabled_and_sits_behind_content() -> None:
    on = render_record(PACK, invoice(simple_lines(), render_profile=profile(has_watermark=True)))
    off = render_record(PACK, invoice(simple_lines(), render_profile=profile(has_watermark=False)))
    assert 'class="watermark"' in on
    assert "position: fixed" in on              # repeats per printed page, out of flow
    assert 'class="watermark"' not in off


def test_watermark_reaches_the_standalone_archetypes_too() -> None:
    for archetype in ("banner-header-06", "fullbleed-05", "boxed-form-01", "receipt-compact-01"):
        inv = invoice(simple_lines(),
                      render_profile=profile(archetype=archetype, has_watermark=True))
        assert 'class="watermark"' in render_record(PACK, inv), archetype


# ── variation matrix: modifiers, not new templates ──────────────────────────
def test_top_row_meta_gets_the_banner_modifier() -> None:
    from docsynth.packs.invoice import body_classes
    inv = invoice(simple_lines(), render_profile=profile(meta_position="top-row"))
    assert "meta-banner" in body_classes(inv)
    assert "meta-banner" in render_record(PACK, inv)


# ── archetype variety across the roster & sampled invoices ──────────────────
ALL_ARCHETYPES = {
    "meta-sidebar-01", "banner-header-06", "boxed-form-01", "fullbleed-05",
    "receipt-compact-01", "telecom-itemized-37",
}


def test_roster_spreads_across_archetypes() -> None:
    arch = Counter(c.render_profile.archetype for c in SeedCatalogue().roster().companies)
    assert set(arch) == ALL_ARCHETYPES
    assert arch["meta-sidebar-01"] > arch["receipt-compact-01"]   # flat is the majority


def test_all_archetypes_appear_and_render_in_sampled_invoices() -> None:
    sampler = InvoiceSampler(max_line_items=80)
    seen: set[str] = set()
    for i in range(6000):
        inv = sampler.generate("rv", i)
        a = inv.render_profile.archetype
        if a not in seen:
            assert render_record(PACK, inv).startswith("<!doctype html>")
            seen.add(a)
        if seen == ALL_ARCHETYPES:
            break
    assert seen == ALL_ARCHETYPES


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


def test_line_parent_class_only_on_rows_that_have_tiers() -> None:
    """`line-parent` carries `break-after: avoid`, to keep a parent row with the
    tier sub-rows beneath it. Put on every row (the old default) it chains an
    unbreakable run across the whole table, so a long invoice's items cannot start
    on page 1 and jump to page 2, leaving it blank. A row with no tiers must not
    claim it."""
    from tests.factories import tiered_line

    plain = _body_only(render_record(
        PACK, invoice(simple_lines(), render_profile=profile(archetype="meta-sidebar-01"))))
    assert 'class="line-parent' not in plain          # no tiers → no break-after chain

    tiered = _body_only(render_record(
        PACK, invoice([tiered_line()], render_profile=profile(archetype="meta-sidebar-01"))))
    assert 'class="line-parent' in tiered             # a real tier parent keeps it
    assert 'class="tier"' in tiered
