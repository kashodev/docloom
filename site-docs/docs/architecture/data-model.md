# Data model & the golden set

docloom's output is two things that must agree: the **documents** (what an
extractor reads) and the **golden set** (the ground truth to score it against).
The golden set is not extracted from the documents — it is the **computed input**
the documents are rendered from, which is what makes it exact.

## The golden record

Every document is a projection of a [`GoldenRecord`](contract.md). For invoices
that is a `GoldenInvoice` — issuer/customer parties, line items (with tiers,
discounts, usage), tax buckets, and reconciled totals, all cent-exact
([money](../core/misc.md)). `to_rows()` flattens it to **named tables**:

- `invoices` — one row per document (header, parties, totals, tax).
- `line_items` — one row per **printed** line (a tiered line contributes its
  parent plus a row per band).

The 1:1-with-the-page rule matters: per-row precision/recall between the golden set
and an extractor's output is only comparable if the rows correspond to what
actually appears. A record that fails to reconcile refuses to render, so a wrong
number never reaches either side.

## No PII in the data

Identifying details (addresses, phones, tax registrations) are **derived
deterministically at generation** (`derive_identity`) and **never stored** in the
catalogue artifact; generated descriptions are screened for PII/quality. The golden
rows are fictional but format-valid — there is no PII-shaped field to leak
(**① Realistic — but no PII**).

## Provenance & self-description

- Each golden row carries the **`catalogue_version`** it was generated against, so
  a corpus is traceable to its content build.
- A run is **self-describing from the bucket alone** via the
  [manifest](../core/pipeline.md): a part per unit lists every document and shard
  with a sha256; the root indexes the parts plus totals. A separate consumer can
  reconstruct and verify the whole run from the manifest — no state store needed
  (`read_run_manifest`, `verify_run`, `enumerate_document_keys`).

## Where it goes

The golden shards ([gzipped JSONL](../core/pipeline.md)) are exported unchanged to a
queryable [sink](../core/storage.md) — Parquet / DuckDB / BigQuery — where the
`invoices` and `line_items` tables (plus `llm_usage`, if a build recorded any)
become the dataset you score against. See [Export](../invoice/export.md).
