"""The golden-record contract.

Every document type produces a *record* — the computed ground truth — and every
record must be able to flatten itself into one or more named tables. That single
method is what makes the export path document-agnostic: an invoice yields
``{"invoices": [...], "line_items": [...]}``; a contract might yield
``{"contracts": [...], "clauses": [...], "parties": [...]}``. The exporter never
needs to know which.

Records are *computed*, never extracted. The record is the input to rendering
and the document is a projection of it, so a record that fails to reconcile must
refuse to exist rather than reach a PDF.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from docloom.core.locale.enums import Locale

# One table name -> the rows destined for it. Rows are plain dicts so any sink
# (Parquet, CSV, BigQuery, DuckDB) can consume them without knowing the pack.
TableRows = dict[str, list[dict[str, Any]]]


@runtime_checkable
class GoldenRecord(Protocol):
    """Minimal surface the kernel requires of any document's ground truth.

    Deliberately tiny. Packs carry whatever else they need — an invoice has
    totals and tax buckets, a contract would have parties and effective dates —
    and the kernel stays ignorant of all of it.
    """

    record_id: str
    run_id: str
    locale: Locale

    def to_rows(self) -> TableRows:
        """Flatten to named tables, one row per *printed* row.

        Golden rows should correspond 1:1 with what appears on the page. A
        tiered invoice line prints a parent row plus a row per band, so it
        contributes all of them — otherwise per-row recall and precision are
        not comparable between the golden set and an extractor's output.
        """
        ...
