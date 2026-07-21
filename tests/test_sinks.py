"""Golden-sink tests.

The load-bearing property is *exactness*: a Decimal in the record must be the
same Decimal after a round trip through Parquet and back out of a SQL engine.
The end-to-end DuckDB test runs the actual evaluation shape — golden JOIN
extracted on record_id — and asserts a one-cent difference is detected.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from docloom.core.sinks import ParquetSink, open_sink
from docloom.core.sinks.arrow import rows_to_table
from docloom.core.sinks.duckdb_sink import DuckDBSink
from tests.factories import invoice, simple_lines, tiered_line


# ─────────────────────────────────────────────────────────────────────────────
# Arrow conversion — the exactness guarantees
# ─────────────────────────────────────────────────────────────────────────────

def test_decimal_becomes_decimal_not_double() -> None:
    table = rows_to_table([{"amount": D("327.02")}, {"amount": D("1234567.89")}])
    field = table.schema.field("amount")
    assert str(field.type).startswith("decimal128")
    assert table.column("amount").to_pylist() == [D("327.02"), D("1234567.89")]


def test_decimal_scale_is_the_widest_in_the_column() -> None:
    """Money (2dp) and AI rates (4dp) coexist without truncating either."""
    table = rows_to_table([{"rate": D("105.00")}, {"rate": D("0.0028")}])
    assert table.schema.field("rate").type.scale == 4
    assert table.column("rate").to_pylist() == [D("105.0000"), D("0.0028")]


def test_bool_is_not_miscast_to_int() -> None:
    """bool is a subclass of int; getting the order wrong yields 0/1."""
    table = rows_to_table([{"is_credit": True}, {"is_credit": False}])
    assert table.schema.field("is_credit").type == __import__("pyarrow").bool_()


def test_date_column_is_date32() -> None:
    table = rows_to_table([{"issue_date": date(2026, 7, 15)}])
    assert str(table.schema.field("issue_date").type) == "date32[day]"


def test_all_none_column_is_nullable_string() -> None:
    table = rows_to_table([{"due_date": None}, {"due_date": None}])
    assert table.schema.field("due_date").type == __import__("pyarrow").string()


def test_nullable_column_infers_from_first_non_null_row() -> None:
    """A column whose first row is None must still get the right type."""
    table = rows_to_table([{"x": None}, {"x": D("9.99")}])
    assert str(table.schema.field("x").type).startswith("decimal128")


def test_list_column_supported() -> None:
    table = rows_to_table([{"billing_models": ["flat_rate", "usage"]}])
    assert str(table.schema.field("billing_models").type) == "list<item: string>"


# ─────────────────────────────────────────────────────────────────────────────
# Parquet sink
# ─────────────────────────────────────────────────────────────────────────────

def test_parquet_write_creates_readable_files(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    rows = invoice(simple_lines()).to_rows()
    sink.write("invoices", rows["invoices"])
    sink.write("line_items", rows["line_items"])
    back = pq.read_table(sink.glob("invoices").replace("*.parquet", ""))
    assert back.num_rows == 1


def test_parquet_write_is_append_only(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    sink.write("t", [{"a": 1}])
    sink.write("t", [{"a": 2}])
    parts = list((tmp_path / "t").glob("*.parquet"))
    assert len(parts) == 2  # one part per write, like streamed shards


def test_empty_write_is_a_noop(tmp_path: Path) -> None:
    ParquetSink(tmp_path).write("t", [])
    assert not (tmp_path / "t").exists()


def test_factory_defaults_to_parquet(tmp_path: Path) -> None:
    assert isinstance(open_sink(str(tmp_path)), ParquetSink)


def test_factory_bare_db_path_is_duckdb(tmp_path: Path) -> None:
    """`--sink out/golden.db` resolves to DuckDB relatively; a Parquet dir
    otherwise."""
    from docloom.core.sinks.duckdb_sink import DuckDBSink

    assert isinstance(open_sink(str(tmp_path / "golden.db")), DuckDBSink)
    assert isinstance(open_sink(str(tmp_path / "golden")), ParquetSink)


def test_factory_bigquery_needs_gcp_extra() -> None:
    # A GCS staging location is required; reaching it (or the BQ client) needs
    # the GCP extra, so the actionable message fires either way.
    with pytest.raises(ImportError, match=r"docloom\[gcp\]"):
        open_sink("bigquery://my-project/docloom_golden?staging=gs://bucket/staging")


def test_factory_bigquery_without_staging_explains_why() -> None:
    with pytest.raises(ValueError, match="staging"):
        open_sink("bigquery://my-project/docloom_golden")


# ─────────────────────────────────────────────────────────────────────────────
# DuckDB — the local evaluation engine
# ─────────────────────────────────────────────────────────────────────────────

def test_duckdb_registers_views_and_queries(tmp_path: Path) -> None:
    sink = DuckDBSink(tmp_path / "golden.db")
    rows = invoice([tiered_line()]).to_rows()
    sink.write("invoices", rows["invoices"])
    sink.write("line_items", rows["line_items"])
    sink.register()

    assert sink.query("SELECT COUNT(*) FROM invoices")[0][0] == 1
    # The tiered line prints a parent row plus three bands.
    assert sink.query("SELECT COUNT(*) FROM line_items")[0][0] == 4
    sink.close()


def test_duckdb_preserves_decimals_through_sql(tmp_path: Path) -> None:
    sink = DuckDBSink(tmp_path / "golden.db")
    sink.write("invoices", invoice(simple_lines()).to_rows()["invoices"])
    sink.register()
    total = sink.query("SELECT grand_total FROM invoices")[0][0]
    assert total == D("210.00")
    sink.close()


def test_evaluation_join_detects_a_one_cent_error(tmp_path: Path) -> None:
    """The whole reason decimals are preserved: the evaluation JOIN must catch
    a one-cent discrepancy, not float it away."""
    sink = DuckDBSink(tmp_path / "golden.db")
    golden = invoice(simple_lines())
    sink.write("invoices", golden.to_rows()["invoices"])

    # An "extracted" table that is correct except for one cent on the total.
    extracted = dict(golden.to_rows()["invoices"][0])
    extracted["grand_total"] = D("210.01")
    sink.write("extracted", [extracted])
    sink.register()

    matches = sink.query(
        "SELECT COUNT(*) FROM invoices g JOIN extracted e USING (invoice_id) "
        "WHERE g.grand_total = e.grand_total"
    )[0][0]
    assert matches == 0   # the one-cent error is caught, not rounded away
    sink.close()
