# docloom

Weave templates and generated data into realistic documents — and get a
**computed golden dataset** alongside them, exact to the cent, to score an
OCR/LLM extraction pipeline against.

The first document type is **invoices**: hundreds of thousands of them, across
dozens of business types, billing models (flat, tiered, metered, subscription,
usage-based), jurisdictions, languages, and capture conditions (clean, scanned,
handwritten). The architecture is not invoice-shaped, though — a
document-agnostic **kernel** does the weaving and a **pack** supplies one
document type. Contracts and legal documents are additional packs, not forks.

## Local-first

The whole pipeline — generate, render, export, evaluate — runs with **no cloud
account**:

```bash
pip install docloom
playwright install chromium      # one-time, for PDF rendering
```

| Concern | Default | Cloud (optional) |
|---|---|---|
| Storage | `file://` filesystem | `gs://` · `s3://` |
| Run state | `sqlite://` | `firestore://` |
| Golden export | `parquet://` · `duckdb://` | `bigquery://` |

Point a config at cloud URIs and install the matching extra
(`pip install 'docloom[gcp]'`) when you want to scale — the calling code does
not change.

## The golden set is exact

A value that is `Decimal("327.02")` in the record is still exactly `327.02`
after a round trip through Parquet and out of SQL. Evaluation is one join:

```sql
SELECT g.template_id, g.locale,
       COUNTIF(e.grand_total = g.grand_total) / COUNT(*) AS accuracy
FROM golden.invoices g JOIN extracted.invoices e USING (invoice_id)
GROUP BY 1, 2
```

The same query runs against a local DuckDB view and a BigQuery external table.

## Extending

- **New business type / billing model / locale** — a table entry or a small
  file in the invoice pack; no kernel change.
- **New document type** — a new pack implementing `DocumentPack` +
  `GoldenRecord`. Register it in-tree or ship it as `docloom-yourpack` via the
  `docloom.packs` entry-point group.

See [DESIGN.md](DESIGN.md) for the full architecture and rationale.

## Status

Early. Kernel, invoice pack, localisation, rendering, and the local
storage/state/sink layer are built and tested (115 tests). Generation
orchestration, catalogue generation, PDF rendering, and cloud adapters are in
progress.
