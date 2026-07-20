"""Golden-dataset sinks — the export target for evaluation.

A run writes golden shards to blob storage as it generates; export reads those
shards and lands them somewhere an evaluation query can join against the
extractor's output. That target is a :class:`GoldenSink`: Parquet on disk,
DuckDB, or BigQuery, chosen by config.

The evaluation contract is a single JOIN on ``record_id`` between the golden
table and the extracted table. For that to be exact, monetary values must
survive as decimals, not floats — a one-cent float drift would score a correct
extraction as wrong. Sinks therefore preserve ``Decimal`` as a decimal column
type, never a double.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GoldenSink(Protocol):
    """A queryable destination for golden tables."""

    def write(self, table: str, rows: Iterable[dict[str, Any]]) -> str:
        """Append ``rows`` to ``table``. Returns where they landed.

        Rows are the flat dicts a record's ``to_rows`` produced. All rows for a
        table share one schema (guaranteed by the pack's row builders and its
        contract tests), so the sink may infer column types from the batch.
        """
        ...

    def register(self) -> None:
        """Make written tables queryable — create views, register external
        tables, refresh a catalogue. A no-op for sinks whose ``write`` already
        leaves a queryable artefact (plain Parquet)."""
        ...

    def close(self) -> None:
        ...
