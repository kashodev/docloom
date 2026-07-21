"""Export tests — closing the round trip.

Generate a run with the sampler, export its shards to DuckDB, and run the actual
evaluation query shape (golden JOIN extracted on invoice_id). Proves the whole
path — generation → JSONL shards → Parquet → SQL — carries exact Decimals, so a
one-cent extraction error is caught, not floated away.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import docloom.packs  # noqa: F401
from docloom.core import get_pack
from docloom.core.pipeline import HtmlRenderer, create_run, export_run, work_run
from docloom.core.sinks.duckdb_sink import DuckDBSink
from docloom.core.sinks.parquet import ParquetSink
from docloom.core.state.sqlite import SqliteStateStore
from docloom.core.storage.local import LocalBlobStore
from docloom.packs.invoice import InvoiceSampler


def _generate(tmp_path: Path, *, total: int = 30, unit_size: int = 10):  # noqa: ANN202
    state = SqliteStateStore(tmp_path / "runs.db")
    blob = LocalBlobStore(tmp_path / "blobs")
    create_run(state, run_id="r", pack="invoice", config_id="cfg",
               total=total, unit_size=unit_size)
    work_run(state, run_id="r", source=InvoiceSampler(max_line_items=25),
             renderer=HtmlRenderer(get_pack("invoice")), blob=blob)
    return blob


def test_export_discovers_tables_and_counts_rows(tmp_path: Path) -> None:
    blob = _generate(tmp_path, total=30)
    sink = ParquetSink(tmp_path / "golden")
    stats = export_run("r", blob, sink)

    assert set(stats.tables) == {"invoices", "line_items"}
    assert stats.tables["invoices"] == 30          # one row per document
    assert stats.tables["line_items"] > 30         # several lines each
    assert stats.total_rows == sum(stats.tables.values())


def test_export_is_empty_for_an_unknown_run(tmp_path: Path) -> None:
    _generate(tmp_path)
    stats = export_run("no-such-run", LocalBlobStore(tmp_path / "blobs"),
                       ParquetSink(tmp_path / "g2"))
    assert stats.tables == {}


def test_roundtrip_preserves_exact_decimals_through_duckdb(tmp_path: Path) -> None:
    blob = _generate(tmp_path, total=20)
    sink = DuckDBSink(tmp_path / "golden.db")
    export_run("r", blob, sink)

    # grand_total came back as an exact decimal, and the tables are queryable.
    rows = sink.query("SELECT invoice_id, grand_total FROM invoices ORDER BY invoice_id")
    assert len(rows) == 20
    assert all(isinstance(total, D) for _, total in rows)
    sink.close()


def test_evaluation_join_detects_a_one_cent_error_after_export(tmp_path: Path) -> None:
    """The end-to-end payoff: an extractor wrong by one cent scores as wrong,
    through the full generation → shard → Parquet → SQL pipeline."""
    blob = _generate(tmp_path, total=10)
    sink = DuckDBSink(tmp_path / "golden.db")
    export_run("r", blob, sink)

    # Build an "extracted" table: correct except one cent on one invoice.
    conn = sink.connection
    conn.execute("CREATE TABLE extracted AS SELECT invoice_id, grand_total FROM invoices")
    conn.execute(
        "UPDATE extracted SET grand_total = grand_total + 0.01 "
        "WHERE invoice_id = (SELECT MIN(invoice_id) FROM invoices)"
    )
    matches = conn.execute(
        "SELECT COUNT(*) FROM invoices g JOIN extracted e USING (invoice_id) "
        "WHERE g.grand_total = e.grand_total"
    ).fetchone()[0]
    assert matches == 9        # the one tampered invoice is caught
    sink.close()


def test_line_item_table_carries_billing_detail(tmp_path: Path) -> None:
    blob = _generate(tmp_path, total=40)
    sink = DuckDBSink(tmp_path / "golden.db")
    export_run("r", blob, sink)
    # The flat line_items table is where per-row extraction is scored; it must
    # carry the billing columns.
    cols = {c[0] for c in sink.query("DESCRIBE line_items")}
    assert {"billing_model", "kind", "extended_amount", "tier_index"} <= cols
    sink.close()
