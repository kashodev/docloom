"""Bundled-font embedding tests.

Four typeface keys carry a bundled OFL font embedded as base64 @font-face, so
those documents render byte-identically regardless of host fonts. The rest
resolve from their semantic fallback stack. These guard both halves.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from docloom.packs.invoice.fonts import (
    BUNDLED,
    FONT_STACKS,
    TYPEFACE_KEYS,
    font_face_css,
    font_stack,
)

_FILES = Path(__file__).resolve().parents[1] / "src/docloom/packs/invoice/fonts/files"


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_bundled_keys_have_both_weight_files_present_and_valid(key: str) -> None:
    for weight in (400, 700):
        f = _FILES / f"{key}-{weight}.woff2"
        assert f.exists(), f
        assert f.read_bytes()[:4] == b"wOF2", f"{f} is not a woff2"


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_font_stack_leads_with_the_bundled_family(key: str) -> None:
    stack = font_stack(key)
    assert stack.startswith(f"'{BUNDLED[key]}', ")
    # ...and keeps the semantic fallback chain behind it.
    assert FONT_STACKS[key] in stack


def test_font_stack_unbundled_key_is_just_the_fallback() -> None:
    assert font_stack("sans-humanist") == FONT_STACKS["sans-humanist"]
    assert "DL " not in font_stack("sans-humanist")


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_font_face_css_embeds_both_weights_as_decodable_base64(key: str) -> None:
    css = font_face_css(key)
    assert css.count("@font-face") == 2
    assert f"font-family: '{BUNDLED[key]}'" in css
    assert "font-weight: 400" in css and "font-weight: 700" in css
    assert "format('woff2')" in css
    # Every embedded payload decodes and is a real woff2.
    for uri in css.split("data:font/woff2;base64,")[1:]:
        b64 = uri.split(")")[0]
        assert base64.b64decode(b64)[:4] == b"wOF2"


def test_font_face_css_empty_for_unbundled_keys() -> None:
    bundled = set(BUNDLED)
    for key in TYPEFACE_KEYS:
        if key not in bundled:
            assert font_face_css(key) == "", key


def test_embedded_family_names_are_collision_proof() -> None:
    # The DL prefix stops the embedded font aliasing a same-named host font.
    assert all(name.startswith("DL ") for name in BUNDLED.values())
