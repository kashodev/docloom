"""BigQuery sink integration test against a *real* project.

The adapter's Parquet staging, external-table DDL, and query shaping are covered
without a network in test_export (local store + fake client). BigQuery has no
local emulator and external tables can only read real GCS, so end-to-end
verification needs a real project — this test provides it, gated on env vars and
skipped everywhere else.

To run it:

    export DOCLOOM_BQ_PROJECT=your-project
    export DOCLOOM_BQ_DATASET=docloom_ci          # must already exist
    export DOCLOOM_BQ_STAGING=gs://your-bucket/ci  # real GCS the project can read
    pip install 'docloom[gcp]'
    pytest tests/test_bigquery_integration.py

It writes two golden rows, registers the external table, and asserts the
cent-exact NUMERIC survives the Parquet -> external-table round trip (the whole
reason the golden set exists).
"""

from __future__ import annotations

import os
from decimal import Decimal
from urllib.parse import urlparse

import pytest

_PROJECT = os.environ.get("DOCLOOM_BQ_PROJECT")
_DATASET = os.environ.get("DOCLOOM_BQ_DATASET")
_STAGING = os.environ.get("DOCLOOM_BQ_STAGING")

pytestmark = pytest.mark.skipif(
    not (_PROJECT and _DATASET and _STAGING),
    reason="needs a real BigQuery project (DOCLOOM_BQ_PROJECT/DATASET/STAGING)",
)


def _staging_store():  # noqa: ANN202 - real cloud only
    from docloom.core.storage.gcs import GcsBlobStore

    parsed = urlparse(_STAGING)  # gs://bucket/prefix
    return GcsBlobStore(parsed.netloc, parsed.path.lstrip("/"))


def test_numeric_survives_the_external_table_roundtrip() -> None:  # pragma: no cover - real cloud
    from docloom.core.sinks.bigquery import BigQuerySink

    sink = BigQuerySink(_PROJECT, _DATASET, _staging_store())
    sink.write("invoices", [
        {"invoice_id": "inv_1", "grand_total": Decimal("327.02")},
        {"invoice_id": "inv_2", "grand_total": Decimal("1000.00")},
    ])
    sink.register()

    rows = dict(sink.query(
        f"SELECT invoice_id, grand_total FROM `{_PROJECT}`.`{_DATASET}`.`invoices`"
    ))
    # Exact to the cent — Parquet decimal128 -> BigQuery NUMERIC, no float drift.
    assert rows["inv_1"] == Decimal("327.02")
    assert rows["inv_2"] == Decimal("1000.00")
