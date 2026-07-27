"""Bundled-font embedding tests.

Four typeface keys carry a bundled OFL font embedded as base64 @font-face, so
those documents render byte-identically regardless of host fonts. The rest
resolve from their semantic fallback stack. These guard both halves.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from docsynth.core.fonts import (
    BUNDLED,
    FONT_STACKS,
    HANDWRITING_KEYS,
    TYPEFACE_KEYS,
    font_face_css,
    font_faces_css,
    font_stack,
    weights_for,
)

_FILES = Path(__file__).resolve().parents[1] / "src/docsynth/core/fonts/files"


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_bundled_keys_have_every_declared_weight_present_and_valid(key: str) -> None:
    for weight in weights_for(key):
        f = _FILES / f"{key}-{weight}.woff2"
        assert f.exists(), f
        assert f.read_bytes()[:4] == b"wOF2", f"{f} is not a woff2"


def test_body_faces_ship_two_weights_and_mark_faces_one() -> None:
    """Script and display faces have no bold cut; body text needs one."""
    assert weights_for("serif-classic") == (400, 700)
    assert weights_for("signature") == (400,)
    assert all(weights_for(k) == (400,) for k in HANDWRITING_KEYS)


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_font_stack_leads_with_the_bundled_family(key: str) -> None:
    stack = font_stack(key)
    assert stack.startswith(f"'{BUNDLED[key]}', ")
    # ...and keeps a fallback chain behind it, whether a body stack or a
    # handwriting/mark fallback.
    assert FONT_STACKS.get(key, "") in stack
    assert stack.rstrip().endswith(("serif", "sans-serif", "monospace", "cursive"))


def test_font_stack_unbundled_key_is_just_the_fallback() -> None:
    assert font_stack("sans-humanist") == FONT_STACKS["sans-humanist"]
    assert "DL " not in font_stack("sans-humanist")


@pytest.mark.parametrize("key", sorted(BUNDLED))
def test_font_face_css_embeds_every_weight_as_decodable_base64(key: str) -> None:
    css = font_face_css(key)
    weights = weights_for(key)
    assert css.count("@font-face") == len(weights)
    assert f"font-family: '{BUNDLED[key]}'" in css
    assert all(f"font-weight: {w}" in css for w in weights)
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


def test_fonts_live_in_core_not_in_a_pack() -> None:
    """Typography is kernel infrastructure: a second pack must not have to
    duplicate the bundle to get byte-identical rendering."""
    import docsynth.core.fonts as module

    assert module.__name__ == "docsynth.core.fonts"
    assert "packs" not in str(_FILES)


def test_packaging_ships_the_font_files_exactly_once() -> None:
    """Regression: `packages = ["src/docsynth"]` already ships non-.py assets, so a
    `force-include` for them adds a second copy at the same path and the wheel
    build fails outright. Keep the table empty."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.load((root / "pyproject.toml").open("rb"))
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/docsynth"]
    forced = wheel.get("force-include", {})
    assert not forced, f"force-include duplicates packaged assets: {forced}"


def test_embedded_family_names_are_collision_proof() -> None:
    # The DL prefix stops the embedded font aliasing a same-named host font.
    assert all(name.startswith("DL ") for name in BUNDLED.values())


# ── Handwriting faces are a separate pool ───────────────────────────────────
def test_handwriting_faces_are_not_offered_as_body_typefaces() -> None:
    """A company must never be assigned a handwriting face as its body type."""
    assert not set(HANDWRITING_KEYS) & set(TYPEFACE_KEYS)
    for key in ("signature", "stamp"):
        assert key not in TYPEFACE_KEYS


def test_handwriting_keys_are_ordered_least_to_most_messy() -> None:
    """The order is the legibility dial, so it is load-bearing, not cosmetic."""
    assert HANDWRITING_KEYS[0] == "hand-print"      # neatest
    assert HANDWRITING_KEYS[-1] == "hand-scrawl"    # messiest


def test_font_faces_css_embeds_several_faces_at_once() -> None:
    """A handwritten document needs writer + signature + stamp in one file."""
    css = font_faces_css(("hand-casual", "signature", "stamp"))
    assert css.count("@font-face") == 3
    for family in ("DL Caveat", "DL Great Vibes", "DL Oswald"):
        assert f"font-family: '{family}'" in css


def test_font_faces_css_skips_unknown_keys() -> None:
    assert font_faces_css(("signature", "no-such-face")).count("@font-face") == 1
    assert font_faces_css(()) == ""
