"""Golden shards — the ground-truth data written during a run.

As a unit generates, each record's ``to_rows()`` output is appended to a
per-table shard: one gzipped JSONL file per (unit, table), written to blob
storage beside the documents. Export later reads the shards, rebuilds the flat
tables, and lands them in a :class:`GoldenSink`.

The subtlety is **exactness across the JSON round trip.** A subtotal that is
``Decimal("327.02")`` in the record must still be exactly that Decimal after
being written to JSONL and read back — otherwise export would rebuild a Parquet
``decimal128`` from a float or a bare string and the evaluation join would score
correct extractions as wrong. Plain JSON has no Decimal and no date, so this
codec tags them:

    Decimal("327.02")  <->  {"__decimal__": "327.02"}
    date(2026, 7, 15)  <->  {"__date__": "2026-07-15"}

Everything else (str, int, bool, None, list) is native JSON. The tags round-trip
to the exact Python types, so export sees Decimals and dates, not strings.

JSONL (not Parquet) because a shard is bytes to any BlobStore — the same code
path over ``file://``, ``gs://``, ``s3://`` — and one row per line stays
human-inspectable when a golden value looks wrong.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_DECIMAL_TAG = "__decimal__"
_DATE_TAG = "__date__"


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {_DECIMAL_TAG: str(value)}
    if isinstance(value, datetime):        # must precede date
        return {_DATE_TAG: value.isoformat()}
    if isinstance(value, date):
        return {_DATE_TAG: value.isoformat()}
    raise TypeError(f"cannot serialise {type(value).__name__} in a golden shard")


def _object_hook(obj: dict[str, Any]) -> Any:
    if _DECIMAL_TAG in obj:
        return Decimal(obj[_DECIMAL_TAG])
    if _DATE_TAG in obj:
        text = obj[_DATE_TAG]
        return datetime.fromisoformat(text) if "T" in text else date.fromisoformat(text)
    return obj


def encode_shard(rows: Iterable[dict[str, Any]]) -> bytes:
    """Serialise rows to gzipped JSONL, preserving Decimal and date exactly."""
    lines = (json.dumps(row, default=_encode, ensure_ascii=False) for row in rows)
    return gzip.compress("\n".join(lines).encode("utf-8"))


def decode_shard(data: bytes) -> list[dict[str, Any]]:
    """Read a gzipped-JSONL shard back to rows with exact types restored."""
    text = gzip.decompress(data).decode("utf-8")
    if not text:
        return []
    return [json.loads(line, object_hook=_object_hook) for line in text.split("\n")]
