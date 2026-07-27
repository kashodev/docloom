"""Procedural company-stamp tests.

The stamp is an SVG built from the issuer's real details, so the checks are that
it says the right thing, that it is the *same* stamp for a company every time,
and that nothing overflows its ring or box — the failure that makes a seal read
as a broken graphic rather than an impression.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from docsynth.packs.invoice.stamps import INKS, SHAPES, stamp_svg


def a_stamp(**kw):
    base = dict(company="Copperline Halcyon LLC", town="Denver, CO",
                registration="EIN 54-4368743")
    base.update(kw)
    return stamp_svg(**base)


# ── Shape and content ───────────────────────────────────────────────────────
@pytest.mark.parametrize("shape", SHAPES)
def test_every_shape_is_well_formed_svg(shape: str) -> None:
    svg, got = a_stamp(shape=shape)
    assert got == shape
    root = ET.fromstring(svg)              # parses => balanced, quoted, valid
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


@pytest.mark.parametrize("shape", SHAPES)
def test_the_company_details_are_on_the_stamp(shape: str) -> None:
    svg, _ = a_stamp(shape=shape)
    assert "COPPERLINE HALCYON LLC" in svg
    assert "DENVER, CO" in svg
    assert "EIN 54-4368743" in svg


def test_round_seals_curve_their_text_and_carry_stars() -> None:
    for shape in ("circle", "oval"):
        svg, _ = a_stamp(shape=shape)
        assert svg.count("<textPath") == 2      # name on top, town below
        assert svg.count("<polygon") == 2       # a star each side


def test_rect_stamp_is_a_bordered_block_with_no_curved_text() -> None:
    svg, _ = a_stamp(shape="rect")
    assert "<textPath" not in svg
    assert "<rect" in svg


# ── Determinism ─────────────────────────────────────────────────────────────
def test_a_company_always_gets_the_same_stamp() -> None:
    """One office, one die — the design must not vary between invoices."""
    assert a_stamp()[0] == a_stamp()[0]


def test_different_companies_get_different_stamps() -> None:
    one, _ = stamp_svg(company="Northwind Supply", town="New York, NY")
    two, _ = stamp_svg(company="Vantage Harbor SARL", town="Lyon")
    assert one != two


def test_shape_is_chosen_from_the_name_when_not_forced() -> None:
    shapes = {stamp_svg(company=f"Company {i} Ltd", town="Leeds")[1] for i in range(40)}
    assert shapes <= set(SHAPES)
    assert len(shapes) > 1                      # the roster spans shapes


# ── Fitting: the failure that looks broken ──────────────────────────────────
_LONG = "Interprovincial Logistics & Warehousing Company Limited"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("name", ["Ki Ltd", "Northwind Supply", _LONG])
def test_text_is_sized_to_fit_whatever_the_name_length(shape: str, name: str) -> None:
    svg, _ = stamp_svg(company=name, town="Montréal, QC",
                       registration="NEQ 1173829105", shape=shape)
    sizes = [float(m) for m in re.findall(r'font-size="([\d.]+)"', svg)]
    assert sizes, "no text on the stamp"
    # Never so small it vanishes, never so large it must be overflowing.
    assert min(sizes) >= 6.0
    assert max(sizes) <= 32.0


def test_a_very_long_name_is_capped_rather_than_left_to_overflow() -> None:
    svg, _ = stamp_svg(company=_LONG, town="Montréal, QC", shape="rect")
    # Either clipped with an ellipsis or pinned with textLength — never raw.
    assert "…" in svg or "textLength" in svg


def test_short_names_are_letterspaced_not_stretched() -> None:
    """textLength applied unconditionally stretches a short name across the whole
    ring, which looks nothing like a stamp. It must only ever be a cap."""
    svg, _ = stamp_svg(company="Ki Ltd", town="Leeds", shape="circle")
    assert "textLength" not in svg
    spacings = [float(m) for m in re.findall(r'letter-spacing="([\d.]+)"', svg)]
    assert any(s > 1.6 for s in spacings)       # spread out to fill the ring


def test_markup_is_escaped() -> None:
    svg, _ = stamp_svg(company='Ampersand & <Co>', town="Leeds", shape="rect")
    assert "&amp;" in svg and "&lt;CO&gt;" in svg   # the name is stamped in caps
    ET.fromstring(svg)                          # still parses


# ── Ink ─────────────────────────────────────────────────────────────────────
def test_inks_are_desaturated_pad_colours_not_primaries() -> None:
    for family in ("red", "blue"):
        assert INKS[family]
        for colour in INKS[family]:
            assert re.fullmatch(r"#[0-9a-f]{6}", colour)
            assert colour not in {"#ff0000", "#0000ff"}


def test_stamp_draws_in_currentcolor_so_one_ink_drives_every_stroke() -> None:
    svg, _ = a_stamp(shape="circle")
    assert "currentColor" in svg
    # No hard-coded ink inside the mark itself.
    assert not re.search(r'(fill|stroke)="#', svg)
