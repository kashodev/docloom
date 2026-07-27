"""Typography for document rendering — stacks, and a bundled OFL font library.

Kernel-level, not pack-level: a font file and the machinery to embed it say
nothing about invoices. A contract or delivery-note pack wants byte-identical
typography for exactly the same reason this one does, and duplicating a font
bundle per pack would be absurd. What *is* pack business is **selection policy** —
which faces a given document type draws from, and what it does with them; the
invoice pack decides that in its catalogue and its handwriting module.

The earlier design named single web fonts (``Inter``, ``Work Sans``, …) that are
not installed in most render environments, so Chromium fell back to the *same*
generic and every document looked identical. This replaces font *names* with
semantic *stacks*, each with a deliberately distinct character and a fallback
chain that resolves to a different family on macOS, Linux, and the Docker image.
So even without a bundled font, a "serif-elegant" document and a "sans-geometric"
one genuinely differ.

A record's typeface key (for invoices, ``profile.typeface``) is recorded in the
golden data, which makes it a useful axis to slice an evaluation by.
:func:`font_stack` resolves a key to the CSS ``font-family`` value.

For byte-identical rendering across machines, four of the stacks are backed by
a **bundled OFL font** embedded as base64 ``@font-face`` (see :data:`BUNDLED`):
Inter, Noto Serif, Zilla Slab, and JetBrains Mono, at weights 400 and 700. When
a document uses one of those typefaces, :func:`font_stack` prepends the embedded
family and :func:`font_face_css` emits the ``@font-face`` rules, so Chromium
renders from the bundled file rather than whatever the host happens to have. The
remaining stacks still resolve from the fallback chain — visible variety without
the byte-for-byte guarantee. Four handwriting faces plus a signature script and
a stamp face are bundled the same way, for packs that render hand-filled forms.

The font files live in ``core/fonts/files/`` and are redistributed under the SIL
Open Font License 1.1 (see ``core/fonts/OFL.txt`` and
``core/fonts/ATTRIBUTION.md``).
"""

from __future__ import annotations

import base64
from functools import cache
from pathlib import Path

_GENERIC_FALLBACK = "sans-serif"
_FILES = Path(__file__).parent / "files"

# key -> a CSS font stack. Each mixes macOS, Linux, and Windows options so the
# family that resolves is distinct per key on any of them.
FONT_STACKS: dict[str, str] = {
    "serif-classic":
        "Georgia, 'Times New Roman', 'Liberation Serif', 'Noto Serif', serif",
    "serif-elegant":
        "Palatino, 'Palatino Linotype', 'Book Antiqua', 'URW Palladio L', 'DejaVu Serif', serif",
    "serif-transitional":
        "Baskerville, 'Libre Baskerville', 'Times New Roman', 'Liberation Serif', serif",
    "sans-neutral":
        "'Helvetica Neue', Helvetica, Arial, 'Liberation Sans', 'Noto Sans', sans-serif",
    "sans-humanist":
        "'Trebuchet MS', 'Segoe UI', Verdana, 'DejaVu Sans', 'Noto Sans', sans-serif",
    "sans-geometric":
        "'Century Gothic', 'Avenir Next', Futura, 'URW Gothic', 'Noto Sans', sans-serif",
    "sans-grotesque":
        "'Arial Narrow', 'Liberation Sans Narrow', 'Archivo Narrow', "
        "'Roboto Condensed', sans-serif",
    "slab":
        "Rockwell, 'Roboto Slab', 'Rockwell Nova', 'DejaVu Serif', serif",
    "mono-invoice":
        "'Courier New', Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace",
    "sans-gill":
        "'Gill Sans', 'Gill Sans MT', Calibri, 'DejaVu Sans', 'Noto Sans', sans-serif",
}

#: Ordered *body* typeface keys, for the catalogue to draw from. Deliberately
#: only the text faces — a company never gets a handwriting face as its body type.
TYPEFACE_KEYS: tuple[str, ...] = tuple(FONT_STACKS)

#: Fallback chains for the handwriting/mark faces. Kept out of FONT_STACKS so
#: they stay out of TYPEFACE_KEYS, but still resolve to *something* script-like
#: if a bundled file is ever missing.
_MARK_FALLBACKS: dict[str, str] = {
    "hand-casual": "'Bradley Hand', 'Segoe Script', cursive",
    "hand-print": "'Comic Sans MS', 'Segoe Print', cursive",
    "hand-neat": "'Segoe Script', 'Bradley Hand', cursive",
    "hand-scrawl": "'Segoe Print', 'Comic Sans MS', cursive",
    "signature": "'Snell Roundhand', 'Brush Script MT', cursive",
    "stamp": "'Haettenschweiler', 'Arial Narrow', Impact, sans-serif",
}

#: Typeface keys with a bundled OFL font -> the embedded ``font-family`` name.
#: A ``DL`` prefix keeps the embedded family from colliding with a host font of
#: the same name, so the bundle is used verbatim rather than a local variant.
BUNDLED: dict[str, str] = {
    "serif-classic": "DL Noto Serif",     # SIL OFL 1.1
    "sans-neutral": "DL Inter",           # SIL OFL 1.1
    "slab": "DL Zilla Slab",              # SIL OFL 1.1
    "mono-invoice": "DL JetBrains Mono",  # SIL OFL 1.1
    # ── Handwriting faces, for the handwritten-form archetype ───────────────
    # Each reads as a different *person* filling in the pad, so a corpus of
    # handwritten invoices is not all in one hand.
    "hand-casual": "DL Caveat",           # SIL OFL 1.1 — quick, slanted
    "hand-print": "DL Patrick Hand",      # SIL OFL 1.1 — neat block printing
    "hand-neat": "DL Shadows Into Light",  # SIL OFL 1.1 — tidy cursive-ish
    "hand-scrawl": "DL Reenie Beanie",    # SIL OFL 1.1 — loose, least legible
    # Signature and rubber stamp are marks, not body text — one face each.
    "signature": "DL Great Vibes",        # SIL OFL 1.1 — flowing script
    "stamp": "DL Oswald",                 # SIL OFL 1.1 — condensed, stamp-like
}

#: The handwriting faces a "writer" is drawn from, ordered least to most messy.
#: Doubles as a legibility dial: picking further down this list makes a document
#: harder to read, which is the controllable difficulty axis for OCR/HTR eval.
HANDWRITING_KEYS: tuple[str, ...] = (
    "hand-print", "hand-neat", "hand-casual", "hand-scrawl",
)
SIGNATURE_KEY = "signature"
STAMP_KEY = "stamp"

_BUNDLED_WEIGHTS: tuple[int, ...] = (400, 700)
#: Faces bundled at a single weight — script and display faces have no bold cut.
_SINGLE_WEIGHT: frozenset[str] = frozenset(
    (*HANDWRITING_KEYS, SIGNATURE_KEY, STAMP_KEY)
)


def family_for(key: str) -> str:
    """The embedded ``font-family`` name for a bundled key (quoted for CSS)."""
    return BUNDLED.get(key, "sans-serif")


def font_stack(key: str) -> str:
    """Resolve a typeface key to a CSS ``font-family`` value.

    When the key has a bundled font, its embedded family leads the stack so the
    bundled file wins; the semantic fallback chain follows for environments that
    somehow lack it. Falls back to a neutral sans for an unknown key rather than
    raising, so a catalogue that names a stack this version does not know still
    renders.
    """
    fallback = (
        FONT_STACKS.get(key)
        or _MARK_FALLBACKS.get(key)
        or FONT_STACKS.get("sans-neutral", _GENERIC_FALLBACK)
    )
    family = BUNDLED.get(key)
    return f"'{family}', {fallback}" if family else fallback


@cache
def _data_uri(key: str, weight: int) -> str:
    """base64 ``data:`` URI for one bundled weight. Cached — the same handful of
    fonts is embedded across an entire run, so read+encode each at most once."""
    raw = (_FILES / f"{key}-{weight}.woff2").read_bytes()
    return "data:font/woff2;base64," + base64.b64encode(raw).decode("ascii")


def weights_for(key: str) -> tuple[int, ...]:
    """Bundled weights for a key — script/display faces ship regular only."""
    return (400,) if key in _SINGLE_WEIGHT else _BUNDLED_WEIGHTS


@cache
def font_faces_css(keys: tuple[str, ...]) -> str:
    """``@font-face`` rules for several keys at once.

    The handwritten archetype needs three faces in one document (the writer's
    hand, the signature script, the stamp), so embedding is not one-per-document.
    Unknown keys are skipped rather than raising.
    """
    return "\n".join(css for key in keys if (css := font_face_css(key)))


@cache
def font_face_css(key: str) -> str:
    """``@font-face`` rules that embed the bundled OFL font for this key.

    Returns base64-embedded ``@font-face`` declarations (every bundled weight) so
    the rendered HTML is self-contained and byte-identical everywhere, or an
    empty string for a key with no bundled font (it renders from the stack).
    """
    family = BUNDLED.get(key)
    if not family:
        return ""
    return "\n".join(
        f"@font-face {{ font-family: '{family}'; font-style: normal; "
        f"font-weight: {weight}; font-display: swap; "
        f"src: url({_data_uri(key, weight)}) format('woff2'); }}"
        for weight in weights_for(key)
    )
