"""Parquet sink — the local-first golden target.

Writes one Parquet part-file per ``write`` call under ``<root>/<table>/``. Many
parts per table is intentional: export streams shards, and appending a part is
cheaper and more crash-safe than rewriting a growing file. Any Parquet reader —
DuckDB, Polars, pandas, BigQuery external tables — reads the directory as one
table via a ``*.parquet`` glob.

``register`` is a no-op: a directory of Parquet files is already queryable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from docsynth.core.sinks.arrow import rows_to_table


class ParquetSink:
    """A :class:`~docsynth.core.sinks.base.GoldenSink` writing Parquet to disk."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def table_dir(self, table: str) -> Path:
        return self._root / table

    def write(self, table: str, rows: Iterable[dict[str, Any]]) -> str:
        batch = list(rows)
        if not batch:
            return self.table_dir(table).as_uri()
        out_dir = self.table_dir(table)
        out_dir.mkdir(parents=True, exist_ok=True)
        # A random part name lets concurrent export workers write the same table
        # without coordinating; readers glob the directory regardless.
        part = out_dir / f"part-{uuid.uuid4().hex}.parquet"
        pq.write_table(rows_to_table(batch), part)
        return part.as_uri()

    def register(self) -> None:
        """No-op — the Parquet directory is already a queryable table."""

    def close(self) -> None:
        pass

    def glob(self, table: str) -> str:
        """The read glob for a table, e.g. for ``read_parquet`` in DuckDB."""
        return str(self.table_dir(table) / "*.parquet")
