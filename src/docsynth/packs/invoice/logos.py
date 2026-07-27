"""Procedural, key-free brand marks.

A small monochrome SVG monogram derived deterministically from the company
name. Every shape is drawn in ``currentColor``, so the same mark takes the
accent colour on a light body and reverses to white inside a coloured banner
with no per-archetype knowledge — the CSS that colours the wordmark colours the
mark. No network and no keys: this is the local-first identity path. Richer,
LLM-designed marks can drop in later behind the same ``m.logo`` macro without
touching any template.

Determinism is load-bearing: a vendor's mark must be identical across every one
of its invoices and across worker processes, so the shape is chosen from a
stable SHA-256 of the name, never the salted built-in ``hash()``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

_VIEW = 48  # SVG user-units; the mark is a square drawn in a 48×48 box.


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def initials(name: str, limit: int = 2) -> str:
    """Up to ``limit`` leading letters, one per word — the monogram text.

    Falls back to the first character, then a bullet, so a mark is always drawn
    for a name of punctuation or emptiness rather than raising.
    """
    words = [w for w in name.replace(".", " ").replace("-", " ").split() if w[:1].isalnum()]
    letters = "".join(w[0] for w in words)[:limit]
    if not letters:
        letters = next((ch for ch in name if ch.isalnum()), "•")
    return letters.upper()


def _text(ini: str, size: int = 19) -> str:
    # y is a baseline nudged below centre so the caps sit optically centred.
    return (
        f'<text x="24" y="{24 + size // 3}" text-anchor="middle" font-size="{size}" '
        f'font-weight="700" fill="currentColor">{ini}</text>'
    )


def _circle(ini: str) -> str:
    return (
        f'<circle cx="24" cy="24" r="21" fill="none" stroke="currentColor" '
        f'stroke-width="3"/>{_text(ini)}'
    )


def _diamond(ini: str) -> str:
    return (
        '<rect x="8" y="8" width="32" height="32" transform="rotate(45 24 24)" '
        f'fill="none" stroke="currentColor" stroke-width="3"/>{_text(ini, 16)}'
    )


def _hex(ini: str) -> str:
    return (
        '<polygon points="24,3 42,13.5 42,34.5 24,45 6,34.5 6,13.5" '
        f'fill="none" stroke="currentColor" stroke-width="3"/>{_text(ini, 16)}'
    )


def _shield(ini: str) -> str:
    return (
        '<path d="M24 3 L43 9 V26 Q43 40 24 45 Q5 40 5 26 V9 Z" '
        f'fill="none" stroke="currentColor" stroke-width="3"/>{_text(ini, 16)}'
    )


def _bars(_ini: str) -> str:
    # A letterless abstract mark — three ascending bars, like many logistics marks.
    return (
        '<rect x="6"  y="30" width="8" height="12" fill="currentColor"/>'
        '<rect x="20" y="20" width="8" height="22" fill="currentColor"/>'
        '<rect x="34" y="8"  width="8" height="34" fill="currentColor"/>'
    )


_MARKS: tuple[Callable[[str], str], ...] = (_circle, _diamond, _hex, _shield, _bars)


def logo_mark(name: str) -> str:
    """A monochrome monogram SVG for ``name``, stable across processes."""
    ini = initials(name)
    builder = _MARKS[_seed(name) % len(_MARKS)]
    return (
        f'<svg class="logo-mark" viewBox="0 0 {_VIEW} {_VIEW}" width="{_VIEW}" height="{_VIEW}" '
        f'role="img" aria-label="{ini}">{builder(ini)}</svg>'
    )


def watermark_mark(name: str) -> str:
    """A large faint monogram for the page-background watermark.

    The same monogram as the logo, scaled up; the archetype CSS positions and
    fades it. Drawn in ``currentColor`` too, so a page can tint it by setting a
    colour on ``.watermark``.
    """
    ini = initials(name, limit=3)
    return (
        '<svg class="watermark-mark" viewBox="0 0 200 200" width="200" height="200" '
        f'role="presentation" aria-hidden="true">'
        f'<text x="100" y="132" text-anchor="middle" font-size="120" font-weight="800" '
        f'fill="currentColor">{ini}</text></svg>'
    )
