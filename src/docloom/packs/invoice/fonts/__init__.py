"""Typeface stacks for invoice rendering.

The earlier design named single web fonts (``Inter``, ``Work Sans``, …) that are
not installed in most render environments, so Chromium fell back to the *same*
generic and every invoice looked identical. This replaces font *names* with
semantic *stacks*, each with a deliberately distinct character and a fallback
chain that resolves to a different family on macOS, Linux, and the Docker image.
So even without a bundled font, a "serif-elegant" invoice and a "sans-geometric"
one genuinely differ.

``profile.typeface`` now holds one of these keys (recorded in the golden data,
useful for slicing eval by typeface). :func:`font_stack` resolves a key to the
CSS ``font-family`` value.

For byte-identical rendering across machines, four of the stacks are backed by
a **bundled OFL font** embedded as base64 ``@font-face`` (see :data:`BUNDLED`):
Inter, Noto Serif, Zilla Slab, and JetBrains Mono, at weights 400 and 700. When
a document uses one of those typefaces, :func:`font_stack` prepends the embedded
family and :func:`font_face_css` emits the ``@font-face`` rules, so Chromium
renders from the bundled file rather than whatever the host happens to have. The
remaining stacks still resolve from the fallback chain — visible variety without
the byte-for-byte guarantee. The font files live in ``fonts/files/`` and are
redistributed under the SIL Open Font License 1.1 (see ``fonts/OFL.txt`` and
``fonts/ATTRIBUTION.md``).
"""

from __future__ import annotations

import base64
from functools import lru_cache
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
        "'Arial Narrow', 'Liberation Sans Narrow', 'Archivo Narrow', 'Roboto Condensed', sans-serif",
    "slab":
        "Rockwell, 'Roboto Slab', 'Rockwell Nova', 'DejaVu Serif', serif",
    "mono-invoice":
        "'Courier New', Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace",
    "sans-gill":
        "'Gill Sans', 'Gill Sans MT', Calibri, 'DejaVu Sans', 'Noto Sans', sans-serif",
}

#: Ordered keys, for the catalogue to draw from.
TYPEFACE_KEYS: tuple[str, ...] = tuple(FONT_STACKS)

#: Typeface keys with a bundled OFL font -> the embedded ``font-family`` name.
#: A ``DL`` prefix keeps the embedded family from colliding with a host font of
#: the same name, so the bundle is used verbatim rather than a local variant.
BUNDLED: dict[str, str] = {
    "serif-classic": "DL Noto Serif",     # SIL OFL 1.1
    "sans-neutral": "DL Inter",           # SIL OFL 1.1
    "slab": "DL Zilla Slab",              # SIL OFL 1.1
    "mono-invoice": "DL JetBrains Mono",  # SIL OFL 1.1
}
_BUNDLED_WEIGHTS: tuple[int, ...] = (400, 700)


def font_stack(key: str) -> str:
    """Resolve a typeface key to a CSS ``font-family`` value.

    When the key has a bundled font, its embedded family leads the stack so the
    bundled file wins; the semantic fallback chain follows for environments that
    somehow lack it. Falls back to a neutral sans for an unknown key rather than
    raising, so a catalogue that names a stack this version does not know still
    renders.
    """
    fallback = FONT_STACKS.get(key, FONT_STACKS.get("sans-neutral", _GENERIC_FALLBACK))
    family = BUNDLED.get(key)
    return f"'{family}', {fallback}" if family else fallback


@lru_cache(maxsize=None)
def _data_uri(key: str, weight: int) -> str:
    """base64 ``data:`` URI for one bundled weight. Cached — the same handful of
    fonts is embedded across an entire run, so read+encode each at most once."""
    raw = (_FILES / f"{key}-{weight}.woff2").read_bytes()
    return "data:font/woff2;base64," + base64.b64encode(raw).decode("ascii")


@lru_cache(maxsize=None)
def font_face_css(key: str) -> str:
    """``@font-face`` rules that embed the bundled OFL font for this key.

    Returns base64-embedded ``@font-face`` declarations (weights 400 and 700) so
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
        for weight in _BUNDLED_WEIGHTS
    )
