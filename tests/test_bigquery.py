"""BigQuery sink tests, via a local staging store and a fake BQ client.

The whole adapter is exercised without a cloud account: Parquet is written to a
real (local) BlobStore, and the external-table DDL is asserted against a fake
client that records executed SQL. Injecting a ``GcsBlobStore`` in production is
the only change — the DDL then embeds ``gs://`` URIs instead of ``file://``.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from docsynth.core.sinks.bigquery import BigQuerySink
from docsynth.core.storage.gcs import GcsBlobStore
from docsynth.core.storage.local import LocalBlobStore
from tests.factories import invoice, simple_lines, tiered_line
from tests.fakes import FakeBigQueryClient, FakeGcsClient


def make_sink(tmp_path: Path) -> tuple[BigQuerySink, LocalBlobStore, FakeBigQueryClient]:
    staging = LocalBlobStore(tmp_path / "staging")
    client = FakeBigQueryClient()
    sink = BigQuerySink("proj", "docsynth_golden", staging=staging, client=client)
    return sink, staging, client


def test_write_stages_parquet(tmp_path: Path) -> None:
    sink, staging, _ = make_sink(tmp_path)
    rows = invoice([tiered_line()]).to_rows()
    sink.write("line_items", rows["line_items"])

    keys = list(staging.iter_keys("line_items/"))
    assert len(keys) == 1 and keys[0].endswith(".parquet")
    # The staged Parquet is the flattened rows, tier bands included.
    table = pq.read_table(tmp_path / "staging" / keys[0])
    assert table.num_rows == 4


def test_write_preserves_decimals_in_parquet(tmp_path: Path) -> None:
    """The reason for external tables: decimal128 → NUMERIC keeps the join exact."""
    sink, staging, _ = make_sink(tmp_path)
    sink.write("invoices", invoice(simple_lines()).to_rows()["invoices"])
    key = next(iter(staging.iter_keys("invoices/")))
    table = pq.read_table(tmp_path / "staging" / key)
    assert str(table.schema.field("grand_total").type).startswith("decimal128")


def test_register_emits_external_table_ddl(tmp_path: Path) -> None:
    sink, _, client = make_sink(tmp_path)
    rows = invoice(simple_lines()).to_rows()
    sink.write("invoices", rows["invoices"])
    sink.write("line_items", rows["line_items"])
    sink.register()

    assert len(client.executed) == 2
    joined = "\n".join(client.executed)
    assert "CREATE OR REPLACE EXTERNAL TABLE" in joined
    assert "`proj`.`docsynth_golden`.`invoices`" in joined
    assert "format = 'PARQUET'" in joined


def test_ddl_points_at_the_staging_glob() -> None:
    """The production pairing: GCS staging keeps the ``*`` glob literal, which
    BigQuery requires. (A local store would percent-encode it — see the note in
    the sink; local is for the write/read tests, GCS for the DDL.)"""
    staging = GcsBlobStore("my-bucket", "docsynth/v1", client=FakeGcsClient())
    sink = BigQuerySink("proj", "docsynth_golden", staging=staging,
                        client=FakeBigQueryClient())
    ddl = sink.external_table_ddl("invoices")
    assert "uris = ['gs://my-bucket/docsynth/v1/invoices/*.parquet']" in ddl


def test_empty_write_is_a_noop(tmp_path: Path) -> None:
    sink, staging, client = make_sink(tmp_path)
    sink.write("invoices", [])
    sink.register()
    assert list(staging.iter_keys()) == []
    assert client.executed == []
