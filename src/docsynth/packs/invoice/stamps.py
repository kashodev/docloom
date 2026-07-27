"""Procedural company stamps — the official seal on a hand-filled invoice.

Real office stamps are not a word in a box. They carry the company's actual
identity: the registered name curved around a ring, the town underneath, a
registration number in the middle, a pair of stars flanking it — or, for a
self-inking rectangular stamp, the name in bold caps over an address block. This
module builds those as SVG, deterministically from the issuer, in three shapes:

* ``circle`` — a corporate seal: double ring, name on the top arc, city on the
  bottom arc, a small centred block, stars at the sides.
* ``oval``  — the same idea on an ellipse, with straight middle lines (the
  "for rubber stamps & company seals / tel: …" layout).
* ``rect``  — a rounded self-inking stamp: stacked lines, name largest.

Two things make the output read as *stamped* rather than *drawn*:

* **The text is real.** The issuer's own name, town and registration number, so a
  stamp on Copperline Halcyon's invoice says Copperline Halcyon — an extractor
  can cross-check it against the golden record, which is exactly the sort of
  signal a real corpus has.
* **The ink is uneven.** Nothing is at full opacity, the ring weights differ, and
  the template applies a turbulence filter over the top, so edges break up the
  way a rubber die on a pad does.

SVG rather than a raster asset library: curved text needs ``textPath``, every
company gets its own stamp with no asset to license or repeat, and it stays
key-free and deterministic like the rest of the pack.
"""

from __future__ import annotations

import hashlib
from random import Random
from xml.sax.saxutils import escape

#: Stamp shapes, and how often each is drawn.
SHAPES: tuple[str, ...] = ("circle", "oval", "rect")
_SHAPE_WEIGHTS: tuple[float, ...] = (0.45, 0.2, 0.35)

#: Pad inks. Real stamp pads are violet-blue or a dull brick red, never a pure
#: primary — the slight desaturation is most of what makes them look real.
INKS: dict[str, tuple[str, ...]] = {
    "red": ("#9d2b23", "#a83a2e", "#8c241f", "#b03a30"),
    "blue": ("#1d3f8f", "#24408a", "#1a3576", "#2b4d9c"),
    "violet": ("#4a2a7a", "#553288"),
}

#: What the middle of a round seal says when there is no registration number.
_CENTRE_WORDS: tuple[str, ...] = ("OFFICIAL", "CORPORATE", "COMPANY")


def _seed_of(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


#: Mean glyph advance for bold caps, as a fraction of the font size. Used to size
#: text to the space it has; ``textLength`` then pins the result exactly, so this
#: only has to be close enough to avoid visibly condensed or loose lettering.
_ADVANCE = 0.68


def _fit(text: str, span: float, letter_spacing: float, base: float, floor: float) -> float:
    """Largest font size at which ``text`` fits ``span`` user-units."""
    n = len(text)
    if not n:
        return base
    usable = span - letter_spacing * n
    return max(floor, min(base, usable / (n * _ADVANCE)))


def _spacing_for(text: str, size: float, budget: float, base: float) -> float:
    """Letter-spacing for a label that is *short* for its space.

    Real seals letter-space a short name out to fill the ring rather than leave
    it huddled in the middle — but only so far. Long labels keep the base value.
    """
    n = len(text)
    if n < 2:
        return base
    natural = n * size * _ADVANCE
    target = 0.72 * budget
    if natural >= target:
        return base
    return min(6.0, max(base, (target - natural) / n))


def _length_cap(text: str, size: float, spacing: float, budget: float) -> str:
    """``textLength`` — but only when the label would otherwise overflow.

    Applied unconditionally it *stretches* short text to fill the span, which
    looks nothing like a stamp; as a cap it is purely a safety net for names too
    long to shrink to at the size floor.
    """
    n = len(text)
    if not n:
        return ""
    natural = n * size * _ADVANCE + spacing * n
    if natural <= budget:
        return ""
    return f' textLength="{budget:.1f}" lengthAdjust="spacingAndGlyphs"'


def _point(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    """Point on a circle. 0° = right, 90° = bottom, 270° = top (SVG y grows down)."""
    import math

    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.-") + "…"


def _star(cx: float, cy: float, r: float) -> str:
    """A small five-pointed star — the classic seal flourish."""
    import math

    pts = []
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.42
        angle = -math.pi / 2 + i * math.pi / 5
        pts.append(f"{cx + radius * math.cos(angle):.2f},{cy + radius * math.sin(angle):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="currentColor"/>'


#: Angular span of the text arcs. Stopping short of the horizontal leaves a gap
#: at each side for the stars — the way a real seal is laid out — and stops the
#: name colliding with them.
_TOP_ARC = (195.0, 345.0)     # upper-left → upper-right, over the top
_BOT_ARC = (165.0, 15.0)      # lower-left → lower-right, under the bottom


def _arc(cx: float, cy: float, r: float, *, top: bool) -> tuple[str, float]:
    """Text path around a ring, plus its length.

    Both run left→right so ``startOffset="50%"`` centres the label. The sweep
    flag is what keeps bottom text the right way up — glyph "up" pointing at the
    centre — instead of hanging upside down under the ring.
    """
    import math

    start, end = _TOP_ARC if top else _BOT_ARC
    sweep = 1 if top else 0
    x0, y0 = _point(cx, cy, r, start)
    x1, y1 = _point(cx, cy, r, end)
    span = (end - start) % 360 if top else (start - end) % 360
    length = math.radians(span) * r
    return f"M {x0:.2f},{y0:.2f} A {r},{r} 0 0,{sweep} {x1:.2f},{y1:.2f}", length


def _elliptic_arc(
    cx: float, cy: float, rx: float, ry: float, *, top: bool
) -> tuple[str, float]:
    import math

    start, end = _TOP_ARC if top else _BOT_ARC
    sweep = 1 if top else 0
    x0 = cx + rx * math.cos(math.radians(start))
    y0 = cy + ry * math.sin(math.radians(start))
    x1 = cx + rx * math.cos(math.radians(end))
    y1 = cy + ry * math.sin(math.radians(end))
    span = (end - start) % 360 if top else (start - end) % 360
    # Ramanujan's approximation is overkill here; the mean radius is close enough
    # to size text against.
    length = math.radians(span) * (rx + ry) / 2
    return f"M {x0:.2f},{y0:.2f} A {rx},{ry} 0 0,{sweep} {x1:.2f},{y1:.2f}", length


def _curved(
    path_id: str, text: str, length: float, size: float, spacing: float, weight: int
) -> str:
    """A label bent around an arc, centred, and never allowed past the stars."""
    budget = length * 0.92
    spacing = _spacing_for(text, size, budget, spacing)
    cap = _length_cap(text, size, spacing, budget)
    return (
        f'<text font-size="{size:.1f}" font-weight="{weight}" '
        f'letter-spacing="{spacing:.2f}" fill="currentColor">'
        f'<textPath href="#{path_id}" startOffset="50%" text-anchor="middle"{cap}>'
        f"{escape(text)}</textPath></text>"
    )


def _straight(
    x: float, y: float, text: str, size: float, budget: float, weight: int, *, italic: bool = False
) -> str:
    """A centred straight line of stamp text, capped to ``budget`` wide."""
    cap = _length_cap(text, size, 0.0, budget)
    style = ' font-style="italic"' if italic else ""
    return (
        f'<text x="{x}" y="{y}" text-anchor="middle" font-size="{size:.1f}"{style} '
        f'font-weight="{weight}" fill="currentColor"{cap}>{escape(text)}</text>'
    )


def _circle_seal(name: str, town: str, centre: str, rng: Random) -> str:
    cx = cy = 100.0
    outer, inner = 95.0, 86.0
    name_r, town_r = 74.0, 76.0
    top_path, top_len = _arc(cx, cy, name_r, top=True)
    bot_path, bot_len = _arc(cx, cy, town_r, top=False)
    name_size = _fit(name, top_len, 1.6, 17.0, 7.5)
    town_size = _fit(town, bot_len, 1.2, 13.0, 6.5)
    dotted = rng.random() < 0.5

    ring_inner = (
        f'<circle cx="{cx}" cy="{cy}" r="{inner}" fill="none" stroke="currentColor" '
        f'stroke-width="1.4" stroke-dasharray="{"1.5 3" if dotted else "0"}"/>'
    )
    centre_lines = [line for line in centre.split("|") if line]
    block = ""
    y = cy - (len(centre_lines) - 1) * 9.0
    for i, line in enumerate(centre_lines):
        # Keep the middle block inside the dotted inner ring (r = 54).
        size = _fit(line, 100.0, 0.8, 15.0 if i == 0 else 11.5, 6.0)
        block += _straight(cx, y + 5, line, size, 100.0, 700)
        y += 19.0

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-label="company stamp">
  <g fill="none" stroke="currentColor">
    <circle cx="{cx}" cy="{cy}" r="{outer}" stroke-width="3.4"/>
    {ring_inner}
    <circle cx="{cx}" cy="{cy}" r="54" stroke-width="1" stroke-dasharray="2 3"/>
  </g>
  <path id="arc-top" d="{top_path}" fill="none"/>
  <path id="arc-bot" d="{bot_path}" fill="none"/>
  {_curved("arc-top", name, top_len, name_size, 1.6, 700)}
  {_curved("arc-bot", town, bot_len, town_size, 1.2, 600)}
  {_star(11, cy, 6.5)}{_star(189, cy, 6.5)}
  {block}
</svg>"""


def _oval_seal(name: str, town: str, middle: list[str], rng: Random) -> str:
    cx, cy = 150.0, 100.0
    rx, ry = 143.0, 93.0
    top_path, top_len = _elliptic_arc(cx, cy, rx - 22, ry - 20, top=True)
    bot_path, bot_len = _elliptic_arc(cx, cy, rx - 20, ry - 18, top=False)
    name_size = _fit(name, top_len, 2.0, 24.0, 9.0)
    town_size = _fit(town, bot_len, 0.8, 15.0, 7.0)

    lines = ""
    y = cy - (len(middle) - 1) * 11.0
    for i, line in enumerate(middle):
        size = _fit(line, 210.0, 0.4, 17.0 if i == 0 else 14.0, 7.0)
        lines += _straight(cx, y + 5, line, size, 210.0, 700 if i == 0 else 500)
        y += 23.0

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200" role="img" aria-label="company stamp">
  <g fill="none" stroke="currentColor">
    <ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" stroke-width="3.2"/>
    <ellipse cx="{cx}" cy="{cy}" rx="{rx - 9}" ry="{ry - 8}" stroke-width="1.3"/>
  </g>
  <path id="oarc-top" d="{top_path}" fill="none"/>
  <path id="oarc-bot" d="{bot_path}" fill="none"/>
  {_curved("oarc-top", name, top_len, name_size, 2.0, 700)}
  {_curved("oarc-bot", town, bot_len, town_size, 0.8, 600)}
  {_star(18, cy, 7)}{_star(282, cy, 7)}
  {lines}
</svg>"""


def _rect_stamp(name: str, lines_below: list[str], rng: Random) -> str:
    w, h = 300.0, 122.0
    pad = 9.0
    inner_w = w - 2 * (pad + 12)     # usable width inside the (possibly double) rule
    name_size = _fit(name, inner_w, 1.1, 30.0, 11.0)
    double = rng.random() < 0.5

    body = _straight(w / 2, 46, name, name_size, inner_w, 700)
    y = 68.0
    for i, line in enumerate(lines_below):
        size = _fit(line, inner_w, 0.3, 15.0 if i == 0 else 13.5, 7.0)
        body += _straight(w / 2, y, line, size, inner_w, 600 if i else 500, italic=(i == 0))
        y += 19.0

    inner = (
        f'<rect x="{pad + 5}" y="{pad + 5}" width="{w - 2 * (pad + 5)}" '
        f'height="{h - 2 * (pad + 5)}" rx="6" fill="none" stroke="currentColor" '
        f'stroke-width="1.2"/>' if double else ""
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="company stamp">
  <rect x="{pad}" y="{pad}" width="{w - 2 * pad}" height="{h - 2 * pad}" rx="9"
        fill="none" stroke="currentColor" stroke-width="3.4"/>
  {inner}
  {body}
</svg>"""


def stamp_svg(
    *,
    company: str,
    town: str = "",
    registration: str = "",
    seed: int | None = None,
    shape: str | None = None,
) -> tuple[str, str]:
    """Build a company stamp. Returns ``(svg, shape)``.

    Deterministic from ``seed`` (or the company name), so a vendor's stamp is the
    same on every one of its invoices — the way a real office has one stamp.
    """
    rng = Random(seed if seed is not None else _seed_of(company))
    if shape is None:
        shape = rng.choices(SHAPES, weights=_SHAPE_WEIGHTS, k=1)[0]

    name = _clip(company.upper(), 42)
    town = _clip(town.upper(), 34) if town else ""
    reg = _clip(registration, 26)

    if shape == "circle":
        centre = reg if reg else f"{rng.choice(_CENTRE_WORDS)}|SEAL"
        if reg:
            centre = f"{rng.choice(_CENTRE_WORDS)}|{reg}"
        return _circle_seal(name, town or "OFFICIAL SEAL", centre, rng), shape

    if shape == "oval":
        middle = [w for w in (reg or "OFFICIAL SEAL",) if w]
        return _oval_seal(name, town or "REGISTERED OFFICE", middle, rng), shape

    below = [line for line in (town, reg) if line]
    return _rect_stamp(name, below or ["OFFICIAL"], rng), shape
