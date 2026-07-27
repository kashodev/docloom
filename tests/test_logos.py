"""Procedural brand-mark tests.

The marks must be deterministic across processes (a vendor's mark is fixed
across all its invoices and across workers), monochrome via currentColor (so one
mark reverses to white inside a coloured banner with no per-archetype code), and
always drawable — even from a degenerate name.
"""

from __future__ import annotations

import re

from docsynth.packs.invoice.logos import initials, logo_mark, watermark_mark


def test_initials_take_leading_letters_per_word() -> None:
    assert initials("Northwind Supply") == "NS"
    assert initials("Cedar Ironclad Bluepeak") == "CI"        # capped at 2 by default
    assert initials("Éditions Mirada") == "ÉM"
    assert initials("anchor") == "A"


def test_initials_never_empty() -> None:
    assert initials("") == "•"
    assert initials("...") == "•"                              # punctuation only


def test_logo_mark_is_deterministic_and_process_independent() -> None:
    # Same name → identical mark, every call. (No salted hash().)
    assert logo_mark("Cedar Vantage Ltd") == logo_mark("Cedar Vantage Ltd")
    assert logo_mark("Cedar Vantage Ltd") != logo_mark("Harbor Beacon Inc")


def test_logo_mark_is_monochrome_currentcolor_svg() -> None:
    svg = logo_mark("Meridian Aurora")
    assert svg.startswith("<svg") and "viewBox" in svg
    assert "currentColor" in svg
    # No baked-in colours that would fight the reversing-on-banner trick.
    assert not re.search(r'(fill|stroke)="#', svg)
    assert 'aria-label="MA"' in svg


def test_marks_span_the_shape_repertoire() -> None:
    """Across many names, more than one shape family is drawn — the mark is not
    effectively constant."""
    shapes = set()
    for a in ("Cedar", "Harbor", "Vantage", "Summit", "Beacon", "Kestrel", "Granite", "Ashford"):
        svg = logo_mark(f"{a} Works")
        for tag in ("circle", "polygon", "rect", "path"):
            if f"<{tag}" in svg:
                shapes.add(tag)
    assert len(shapes) >= 2


def test_watermark_is_a_large_faint_monogram() -> None:
    wm = watermark_mark("Northwind Supply Co")
    assert wm.startswith("<svg") and "currentColor" in wm
    assert 'aria-hidden="true"' in wm
    assert ">NSC<" in wm                                       # up to 3 initials
