"""Golden rows → Arrow, preserving exactness.

The delicate part of export. The golden dataset only has value if a value that
was ``Decimal("327.02")`` in the record is still exactly ``327.02`` in the
queryable table — otherwise the evaluation join scores correct extractions as
wrong. So this module maps Python types to Arrow types deliberately rather than
letting inference pick doubles for numbers:

* ``Decimal`` -> ``decimal128``. The scale is the widest seen in the column
  (money is 2dp, tax rates 3dp, AI rates 4dp), so every value fits without loss.
* ``date`` -> ``date32``; ``datetime`` -> ``timestamp``.
* ``bool`` before ``int`` — ``bool`` is a subclass of ``int`` and would
  otherwise be miscast to an integer column.
* an all-``None`` column -> nullable string, since there is no value to infer
  from and a document-agnostic sink cannot know the intended type.
* ``list`` -> ``list<string>`` (e.g. an invoice's ``billing_models``).

Types are decided by scanning the whole batch, not just the first row, because a
nullable column's first row is often ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa

_DECIMAL_PRECISION = 38   # decimal128 max; ample for any invoice figure


def _scale(values: Sequence[Any]) -> int:
    """Widest fractional scale among a column's Decimals (minimum 0)."""
    scale = 0
    for v in values:
        if isinstance(v, Decimal):
            exp = v.as_tuple().exponent
            if isinstance(exp, int) and exp < 0:
                scale = max(scale, -exp)
    return scale


def _field_type(name: str, values: Sequence[Any]) -> pa.DataType:
    sample = next((v for v in values if v is not None), None)
    if sample is None:
        return pa.string()                       # all-None: nothing to infer
    if isinstance(sample, bool):                 # must precede int
        return pa.bool_()
    if isinstance(sample, Decimal):
        return pa.decimal128(_DECIMAL_PRECISION, _scale(values))
    if isinstance(sample, int):
        return pa.int64()
    if isinstance(sample, float):
        return pa.float64()
    if isinstance(sample, datetime):             # must precede date
        return pa.timestamp("us")
    if isinstance(sample, date):
        return pa.date32()
    if isinstance(sample, list):
        return pa.list_(pa.string())
    return pa.string()


def rows_to_table(rows: Sequence[dict[str, Any]]) -> pa.Table:
    """Build one Arrow table from a batch of uniform-schema row dicts.

    Column order follows the first row. Callers guarantee uniform keys — the
    pack's ``to_rows`` builds every row from the same template and a contract
    test enforces it — so a missing key here is a real bug and surfaces as a
    ``KeyError`` rather than being silently filled.
    """
    if not rows:
        raise ValueError("cannot build a table from zero rows")

    columns = list(rows[0])
    fields: list[pa.Field] = []
    arrays: list[pa.Array] = []
    for name in columns:
        values = [row[name] for row in rows]
        dtype = _field_type(name, values)
        fields.append(pa.field(name, dtype))
        arrays.append(pa.array(values, type=dtype))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
