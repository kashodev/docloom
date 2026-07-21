<p align="center">
  <img src="docloom-logo-page-slate.svg" alt="docloom" width="420">
</p>

Weave templates and generated data into realistic documents — and get a
**computed golden dataset** alongside them, exact to the cent, to score an
OCR/LLM extraction pipeline against.

The first document type is **invoices**: hundreds of thousands of them, across
dozens of business types, billing models (flat, tiered, metered, subscription,
usage-based), jurisdictions, languages, and capture conditions (clean, scanned,
handwritten). The architecture is not invoice-shaped, though — a
document-agnostic **kernel** does the weaving and a **pack** supplies one
document type. Contracts and legal documents are additional packs, not forks.

## Running it

```bash
pip install docloom
playwright install chromium      # one-time, for PDF rendering
```

The **infrastructure** is local by default and cloud-optional — the same
interfaces carry either, dispatched by URI scheme, so the calling code does not
change when you scale:

| Concern | Default (local) | Cloud (optional) |
|---|---|---|
| Storage | `file://` filesystem | `gs://` · `s3://` |
| Run state | `sqlite://` | `firestore://` |
| Golden export | `parquet://` · `duckdb://` | `bigquery://` |

Point a config at cloud URIs and install the matching extra
(`pip install 'docloom[gcp]'`) when you want to scale.

### Local-first is a property of the *pack*, not a platform promise

How a pack generates its **content** is what decides whether it can run without a
cloud account or API key:

- **Invoices are local-first.** The invoice pack draws from a procedural,
  built-in seed catalogue and computes every figure deterministically — no LLM,
  no keys — so it produces a full dataset of hundreds of thousands of documents
  on a laptop. This is the pack that ships today.
- **Text-heavy packs are not, at scale.** A contract's realistic clauses,
  recitals, and defined terms are generated natural language; there is no
  procedural substitute at quality, so an **LLM is effectively required**.
  Those packs run against an LLM provider (batched for cost) — a network and a
  key — even though the storage/state/export infrastructure around them still
  defaults to local.

So "no cloud account" is true end to end for invoices, and remains true for the
infrastructure of any pack; it is *not* a guarantee that every future document
type generates offline. The kernel's provider abstraction exists precisely so a
pack can declare a procedural (local) or an LLM-backed source behind the same
run loop.

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

See [DESIGN.md](DESIGN.md) for the full architecture and rationale, and
[docs/concurrency.md](docs/concurrency.md) for the sharding + multi-worker model
(how one run scales across a fleet without a broker or a leader).

## Status

Early. Kernel, invoice pack, localisation, PDF rendering, and the local
storage/state/sink layer are built and tested (250 tests). Generation
orchestration runs end to end locally; catalogue generation and cloud adapters
are in progress.
