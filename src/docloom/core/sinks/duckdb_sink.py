"""DuckDB sink — the local evaluation engine.

Gives the exact evaluation workflow BigQuery gives, on a laptop, for free. It
writes Parquet (reusing :class:`ParquetSink` for the data plane) and then, on
``register``, creates a view per table over that Parquet. The evaluation JOIN —
golden vs extracted on ``record_id`` — then runs as ordinary SQL against a
DuckDB connection, the same query shape that runs against BigQuery in the cloud
path. Decimal columns stay decimal, so the join is exact.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docloom.core.sinks.parquet import ParquetSink

if TYPE_CHECKING:
    import duckdb


class DuckDBSink:
    """A :class:`~docloom.core.sinks.base.GoldenSink` queryable via DuckDB."""

    def __init__(self, database: str | Path, parquet_root: str | Path | None = None) -> None:
        import duckdb

        self._db_path = Path(database).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Parquet lives beside the database unless told otherwise, so the two
        # move together and a view's underlying files are easy to find.
        root = parquet_root or self._db_path.with_suffix(".parquet")
        self._parquet = ParquetSink(root)
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(str(self._db_path))
        self._tables: set[str] = set()

    def write(self, table: str, rows: Iterable[dict[str, Any]]) -> str:
        location = self._parquet.write(table, rows)
        self._tables.add(table)
        return location

    def register(self) -> None:
        """Create a view per written table over its Parquet files."""
        for table in sorted(self._tables):
            glob = self._parquet.glob(table).replace("'", "''")
            self._conn.execute(
                f"CREATE OR REPLACE VIEW {_ident(table)} AS "
                f"SELECT * FROM read_parquet('{glob}')"
            )

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run SQL against the registered views — the evaluation entry point."""
        return self._conn.execute(sql).fetchall()

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    def close(self) -> None:
        self._conn.close()


def _ident(name: str) -> str:
    """Quote a table identifier. Table names come from pack ``table_names`` —
    trusted, but quoted defensively so a name with a reserved word still works."""
    return '"' + name.replace('"', '""') + '"'
