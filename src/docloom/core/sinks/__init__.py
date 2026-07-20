"""Golden sinks: one protocol, backends chosen by URI scheme.

    parquet:///path/to/dir        Parquet files on disk (default)
    ./golden  or  /abs/golden     shorthand for parquet://
    duckdb:///path/to/golden.db   DuckDB — local SQL evaluation
    bigquery://project/dataset    BigQuery  (pip install 'docloom[gcp]')

The default (Parquet + DuckDB) gives the same JOIN-on-``record_id`` evaluation
locally that BigQuery gives in the cloud, with no account required.
"""

from __future__ import annotations

from urllib.parse import urlparse

from docloom.core.sinks.base import GoldenSink
from docloom.core.sinks.parquet import ParquetSink

__all__ = ["GoldenSink", "ParquetSink", "open_sink"]


def open_sink(uri: str) -> GoldenSink:
    """Construct the golden sink named by ``uri``."""
    parsed = urlparse(uri)
    scheme = parsed.scheme or "parquet"

    if scheme == "parquet":
        return ParquetSink(parsed.path if parsed.scheme else uri)

    if scheme == "duckdb":
        from docloom.core.sinks.duckdb_sink import DuckDBSink

        return DuckDBSink(parsed.path)

    if scheme == "bigquery":
        try:
            from docloom.core.sinks.bigquery import BigQuerySink
        except ImportError as exc:
            raise ImportError(
                "bigquery:// sink needs the GCP extra — pip install 'docloom[gcp]'"
            ) from exc
        return BigQuerySink(project=parsed.netloc, dataset=parsed.path.lstrip("/"))

    raise ValueError(f"unsupported sink scheme {scheme!r} in {uri!r}")
