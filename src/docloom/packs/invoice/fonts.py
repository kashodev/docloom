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

For byte-identical rendering across machines, bundle OFL font files and embed
them via ``@font-face`` — the hook is :func:`font_face_css`, empty until fonts
are added (see TODO.md). The stacks below already give visible variety without
it; bundling upgrades that to pixel-level reproducibility.
"""

from __future__ import annotations

_GENERIC_FALLBACK = "sans-serif"

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


def font_stack(key: str) -> str:
    """Resolve a typeface key to a CSS ``font-family`` value.

    Falls back to a neutral sans for an unknown key rather than raising, so a
    catalogue that names a stack this version does not know still renders.
    """
    return FONT_STACKS.get(key, FONT_STACKS.get("sans-neutral", _GENERIC_FALLBACK))


def font_face_css(key: str) -> str:  # noqa: ARG001
    """``@font-face`` rules to embed a bundled OFL font for this key.

    Empty until fonts are bundled — the stacks alone give visible variety. When
    font files are added, this returns base64 ``@font-face`` rules so rendering
    is byte-identical everywhere. See TODO.md.
    """
    return ""
