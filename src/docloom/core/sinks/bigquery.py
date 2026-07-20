"""BigQuery golden sink (``bigquery://project/dataset?staging=gs://…``).

The cloud counterpart of the DuckDB sink, and the same query surface: it lands
golden rows as Parquet and exposes them as tables an evaluation JOIN reads.

It writes Parquet to a GCS staging location (any :class:`BlobStore`) and, on
``register``, creates a BigQuery **external table** per golden table pointing at
that Parquet. External rather than loaded, matching the DuckDB path: BigQuery
reads the Parquet in place, so there is no second copy and no schema to maintain
by hand — and, crucially, Parquet ``decimal128`` maps to BigQuery ``NUMERIC``,
so the cent-exact evaluation join survives the cloud round trip exactly as it
does locally.

The staging store and the BigQuery client are both injectable, so the whole
adapter — Parquet generation, staging layout, and the external-table DDL — is
tested with a local store and a fake client, no cloud account required.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from io import BytesIO
from typing import Any

import pyarrow.parquet as pq

from docloom.core.sinks.arrow import rows_to_table
from docloom.core.storage.base import BlobStore


class BigQuerySink:
    """A :class:`~docloom.core.sinks.base.GoldenSink` over BigQuery external tables."""

    def __init__(
        self,
        project: str,
        dataset: str,
        staging: BlobStore,
        *,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:
                raise ImportError(
                    "bigquery:// sink needs the GCP extra — pip install 'docloom[gcp]'"
                ) from exc
            client = bigquery.Client(project=project)
        self._client = client
        self._project = project
        self._dataset = dataset
        self._staging = staging
        self._tables: set[str] = set()

    def write(self, table: str, rows: Iterable[dict[str, Any]]) -> str:
        batch = list(rows)
        if not batch:
            return self._staging.uri_for(f"{table}/")
        buffer = BytesIO()
        pq.write_table(rows_to_table(batch), buffer)
        key = f"{table}/part-{uuid.uuid4().hex}.parquet"
        uri = self._staging.put(key, buffer.getvalue(), "application/vnd.apache.parquet")
        self._tables.add(table)
        return uri

    def register(self) -> None:
        """Create an external table per written golden table."""
        for table in sorted(self._tables):
            self._client.query(self.external_table_ddl(table)).result()

    def external_table_ddl(self, table: str) -> str:
        """The ``CREATE EXTERNAL TABLE`` statement for one table.

        Split out so it can be asserted directly in tests without a client.
        """
        source = self._staging.uri_for(f"{table}/*.parquet")
        fqtn = f"`{self._project}`.`{self._dataset}`.`{table}`"
        return (
            f"CREATE OR REPLACE EXTERNAL TABLE {fqtn} "
            f"OPTIONS (format = 'PARQUET', uris = ['{source}'])"
        )

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        return [tuple(row.values()) for row in self._client.query(sql).result()]

    def close(self) -> None:
        pass
