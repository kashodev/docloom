"""Handwriting parameters for the handwritten-form archetype.

A handwritten invoice is not a typeset document with a filter over it — it is a
**pre-printed pad filled in by hand**. The printed chrome (letterhead, column
headings, ruled lines, totals box) is type; the values written into it are in
someone's hand. This module decides *whose* hand and *how* messy, deterministically
from the record's seed, so the same invoice always looks the same.

Three things are resolved here:

* **A writer.** One of the bundled handwriting faces plus an ink colour, fixed
  per document. The faces are ordered least to most messy, which makes the
  choice a **legibility dial** — a controllable difficulty axis for OCR/HTR
  evaluation, not just decoration.
* **Per-field jitter.** Real handwriting does not sit exactly on the rule or at a
  constant size. Every written value gets its own small rotation, vertical
  offset and scale, so no two lines align the way typesetting would.
* **The marks.** A signature (a script face, warped and pressure-varied) and a
  rubber stamp (condensed lettering, roughened edges). Both are rendered by the
  browser as part of the document, so they land *under* any subsequent scan
  degradation — the way real ink does.

Crucially none of this touches the record: a handwritten invoice carries exactly
the same computed values, and therefore exactly the same golden rows, as its
clean twin. Only the rendering differs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from random import Random
from typing import Any

from docloom.packs.invoice.fonts import (
    HANDWRITING_KEYS,
    SIGNATURE_KEY,
    STAMP_KEY,
    font_faces_css,
    font_stack,
)

#: Ballpoint and fountain-pen inks people actually fill forms with.
_INKS: tuple[str, ...] = (
    "#1b2a6b",   # blue ballpoint
    "#16215c",   # darker blue
    "#232323",   # black biro
    "#2d2a26",   # soft pencil-black
)

#: Rubber-stamp colours — always a saturated pad ink, never document black.
_STAMP_INKS: tuple[str, ...] = ("#b0322c", "#1d4f8b", "#7a2470")

#: What the stamp says. Localised at the call site would be nicer, but these
#: read as stock office stamps in every locale the pack supports today.
_STAMP_TEXTS: tuple[str, ...] = ("PAID", "RECEIVED", "APPROVED", "ENTERED")

#: Given names + surnames a signature is built from. Synthetic and generic on
#: purpose: a signature must never resemble a real person's.
_FIRST: tuple[str, ...] = ("A", "J", "M", "R", "S", "T", "K", "L", "D", "P")
_LAST: tuple[str, ...] = (
    "Halloran", "Merrick", "Fenwick", "Brandt", "Okoro", "Lindqvist",
    "Vasquez", "Duthie", "Marchetti", "Ashford", "Renaud", "Sowande",
)


@dataclass(frozen=True, slots=True)
class Jitter:
    """One written value's deviation from perfect placement."""

    rotate: float    # degrees
    dy: float        # px off the rule
    scale: float     # relative size

    @property
    def css(self) -> str:
        return (
            f"transform: rotate({self.rotate}deg) translateY({self.dy}px) "
            f"scale({self.scale});"
        )


@dataclass(frozen=True, slots=True)
class Handwriting:
    """Everything the handwritten archetype needs to render one document."""

    writer_key: str
    writer_stack: str
    ink: str
    size_scale: float
    signature_stack: str
    signature_text: str
    signature_rotate: float
    stamp_stack: str
    stamp_text: str
    stamp_ink: str
    stamp_rotate: float
    stamp_top: float
    stamp_left: float
    face_css: str
    ruled_rows: int
    _jitters: tuple[Jitter, ...] = field(default=(), repr=False)

    def jitter(self, index: int) -> Jitter:
        """Deterministic jitter for the value at ``index`` (wraps, so any number
        of fields is covered from a fixed pool)."""
        return self._jitters[index % len(self._jitters)]

    def to_context(self) -> dict[str, Any]:
        return {
            "writer_key": self.writer_key,
            "writer_stack": self.writer_stack,
            "ink": self.ink,
            "size_scale": self.size_scale,
            "signature_stack": self.signature_stack,
            "signature_text": self.signature_text,
            "signature_rotate": self.signature_rotate,
            "stamp_stack": self.stamp_stack,
            "stamp_text": self.stamp_text,
            "stamp_ink": self.stamp_ink,
            "stamp_rotate": self.stamp_rotate,
            "stamp_top": self.stamp_top,
            "stamp_left": self.stamp_left,
            "face_css": self.face_css,
            "ruled_rows": self.ruled_rows,
            "jitter": self.jitter,
        }


#: How many jitter values to pre-draw. Enough that a page of written values never
#: shows an obvious repeat, small enough to stay cheap.
_JITTER_POOL = 37


def handwriting_for(
    seed: int, *, line_count: int = 0, legibility: float | None = None
) -> Handwriting:
    """Resolve the handwriting for one document, deterministically from ``seed``.

    ``legibility`` in [0, 1] biases the writer choice — 1.0 always picks the
    neatest hand, 0.0 the messiest. Left ``None`` it is drawn from the seed, so a
    corpus spans the range. ``line_count`` sizes the pad so there are always a
    few blank ruled lines after the last item, the way a real pad looks.
    """
    rng = Random(seed)

    if legibility is None:
        legibility = rng.random()
    # Map legibility -> an index into the least..most messy ordering.
    span = len(HANDWRITING_KEYS) - 1
    index = min(span, max(0, round((1.0 - legibility) * span)))
    writer_key = HANDWRITING_KEYS[index]

    jitters = tuple(
        Jitter(
            rotate=round(rng.uniform(-1.6, 1.6), 2),
            dy=round(rng.uniform(-2.4, 1.6), 2),
            scale=round(rng.uniform(0.94, 1.08), 3),
        )
        for _ in range(_JITTER_POOL)
    )

    return Handwriting(
        writer_key=writer_key,
        writer_stack=font_stack(writer_key),
        ink=rng.choice(_INKS),
        size_scale=round(rng.uniform(0.95, 1.15), 3),
        signature_stack=font_stack(SIGNATURE_KEY),
        signature_text=f"{rng.choice(_FIRST)}. {rng.choice(_LAST)}",
        signature_rotate=round(rng.uniform(-5.0, 3.0), 2),
        stamp_stack=font_stack(STAMP_KEY),
        stamp_text=rng.choice(_STAMP_TEXTS),
        stamp_ink=rng.choice(_STAMP_INKS),
        stamp_rotate=round(rng.uniform(-17.0, -5.0), 2),
        stamp_top=round(rng.uniform(46.0, 60.0), 1),
        stamp_left=round(rng.uniform(8.0, 26.0), 1),
        # Embed only the three faces this document actually uses.
        face_css=font_faces_css((writer_key, SIGNATURE_KEY, STAMP_KEY)),
        # Enough rules to reach the foot of the pad, so the blank remainder
        # reads as unused pad rather than a document that stopped early.
        ruled_rows=max(line_count + rng.randint(2, 5), 14),
        _jitters=jitters,
    )
