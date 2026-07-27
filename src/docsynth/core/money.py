"""Money arithmetic.

All monetary values in the golden record are ``Decimal`` quantised to two
decimal places using ROUND_HALF_UP. Floats are never used: a float subtotal
that disagrees with the rendered PDF by one cent would be indistinguishable
from a genuine extraction error during evaluation.

Rounding policy is applied at each *documented* step — that is, wherever a
number appears on the page. Intermediate products are kept at full precision
and rounded only when they become a printed line amount, which is what
accounting systems do and therefore what an extractor will see.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

CENTS: Final = Decimal("0.01")
ZERO: Final = Decimal("0.00")


def money(value: Decimal | int | str) -> Decimal:
    """Quantise to 2dp, ROUND_HALF_UP. Use at every printed-value boundary."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def pct(rate: Decimal, base: Decimal) -> Decimal:
    """Apply a percentage rate to a base and quantise the result.

    ``rate`` is expressed as a percentage (``Decimal("9.975")`` for QST), not a
    fraction, because that is how it is printed on the document.
    """
    return money(base * rate / Decimal(100))


def sum_money(values: list[Decimal]) -> Decimal:
    """Sum pre-quantised amounts. Returns 0.00 for an empty list."""
    total = ZERO
    for v in values:
        total += v
    return money(total)
