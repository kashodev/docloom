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

from docloom.core.fonts import (
    HANDWRITING_KEYS,
    SIGNATURE_KEY,
    STAMP_KEY,
    font_faces_css,
    font_stack,
)
from docloom.packs.invoice.stamps import INKS, stamp_svg

#: Ballpoint and fountain-pen inks people actually fill forms with.
_INKS: tuple[str, ...] = (
    "#1b2a6b",   # blue ballpoint
    "#16215c",   # darker blue
    "#232323",   # black biro
    "#2d2a26",   # soft pencil-black
)

#: Where a stamp actually lands. Someone pressing a die onto a finished invoice
#: aims at empty paper near what they are approving — beside the totals, over the
#: signature, in the lower margin — not at a corner, and never dead centre. Each
#: zone is a (left, top) fraction *range* of the printable box, so the draw is
#: varied within a plausible area rather than uniform over the page.
_STAMP_ZONES: tuple[tuple[str, tuple[float, float], tuple[float, float]], ...] = (
    ("beside-totals", (0.50, 0.70), (0.60, 0.71)),
    ("over-signature", (0.52, 0.72), (0.73, 0.83)),
    ("lower-left", (0.07, 0.24), (0.70, 0.82)),
    ("across-middle", (0.28, 0.48), (0.40, 0.55)),
    ("upper-right", (0.54, 0.71), (0.09, 0.19)),
    ("over-items", (0.13, 0.33), (0.33, 0.47)),
)
#: The printable box a placement is measured against (A4 less the page margins).
_BOX_W_MM, _BOX_H_MM = 182.0, 261.0

#: Nominal width and aspect per stamp shape, in mm — a corporate seal is roughly
#: 40mm across, a self-inking rectangle wider and shallower.
_STAMP_GEOMETRY: dict[str, tuple[float, float]] = {
    "circle": (40.0, 1.0),
    "oval": (58.0, 300 / 200),
    "rect": (54.0, 300 / 122),
}

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
    stamp_svg: str
    stamp_shape: str
    stamp_zone: str
    stamp_ink: str
    stamp_rotate: float
    stamp_top: float
    stamp_left: float
    stamp_width: float
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
            "stamp_svg": self.stamp_svg,
            "stamp_shape": self.stamp_shape,
            "stamp_zone": self.stamp_zone,
            "stamp_ink": self.stamp_ink,
            "stamp_rotate": self.stamp_rotate,
            "stamp_top": self.stamp_top,
            "stamp_left": self.stamp_left,
            "stamp_width": self.stamp_width,
            "face_css": self.face_css,
            "ruled_rows": self.ruled_rows,
            "jitter": self.jitter,
        }


#: How many jitter values to pre-draw. Enough that a page of written values never
#: shows an obvious repeat, small enough to stay cheap.
_JITTER_POOL = 37


def _stamp(rng: Random, *, company: str, town: str, registration: str) -> dict[str, Any]:
    """Build the company's stamp and decide where it was pressed.

    The seal's *design* is derived from the company name alone, so one office
    keeps one stamp across all its invoices. Everything about the *impression* —
    which zone it landed in, the angle, how heavily it inked — is per document.
    """
    svg, shape = stamp_svg(
        company=company or "OFFICIAL",
        town=town,
        registration=registration,
        # Design keyed to the company, not the document.
        seed=None if company else rng.getrandbits(32),
    )
    ink_family = rng.choice(("red", "red", "blue", "blue", "violet"))
    ink = rng.choice(INKS[ink_family])

    width, aspect = _STAMP_GEOMETRY[shape]
    width = round(width * rng.uniform(0.92, 1.08), 1)
    height = width / aspect

    zone, (l0, l1), (t0, t1) = rng.choice(_STAMP_ZONES)
    left = rng.uniform(l0, l1) * _BOX_W_MM
    top = rng.uniform(t0, t1) * _BOX_H_MM
    # Keep the whole impression on the paper — a die pressed half off the sheet
    # is a different artefact, and not the one we are modelling.
    left = min(max(left, 4.0), _BOX_W_MM - width - 4.0)
    top = min(max(top, 4.0), _BOX_H_MM - height - 4.0)

    return {
        "stamp_svg": svg,
        "stamp_shape": shape,
        "stamp_zone": zone,
        "stamp_ink": ink,
        # Hand-pressed, so askew — but a die is held roughly square, not spun.
        "stamp_rotate": round(rng.uniform(-15.0, 11.0), 2),
        "stamp_top": round(top, 1),
        "stamp_left": round(left, 1),
        "stamp_width": width,
    }


def handwriting_for(
    seed: int,
    *,
    line_count: int = 0,
    legibility: float | None = None,
    company: str = "",
    town: str = "",
    registration: str = "",
) -> Handwriting:
    """Resolve the handwriting for one document, deterministically from ``seed``.

    ``legibility`` in [0, 1] biases the writer choice — 1.0 always picks the
    neatest hand, 0.0 the messiest. Left ``None`` it is drawn from the seed, so a
    corpus spans the range. ``line_count`` sizes the pad so there are always a
    few blank ruled lines after the last item, the way a real pad looks.

    ``company`` / ``town`` / ``registration`` are stamped into the seal, so the
    mark carries the issuer's real identity rather than a generic word. The stamp
    *design* is keyed off the company alone — one office, one stamp, reused on
    every invoice — while where it lands and how hard it was pressed vary per
    document.
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
        **_stamp(rng, company=company, town=town, registration=registration),
        # Embed only the three faces this document actually uses.
        face_css=font_faces_css((writer_key, SIGNATURE_KEY, STAMP_KEY)),
        # Enough rules to reach the foot of the pad, so the blank remainder
        # reads as unused pad rather than a document that stopped early.
        ruled_rows=max(line_count + rng.randint(2, 5), 14),
        _jitters=jitters,
    )
