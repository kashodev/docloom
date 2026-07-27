<p align="center">
  <img src="docloom-logo-page-slate.svg" alt="docloom" width="420">
</p>

Weave templates and generated data into realistic **synthetic documents** — and
get a **computed golden dataset** alongside them, exact to the cent. The primary
intended use is **testing AI/OCR document-extraction pipelines**: generate a
large, varied corpus of synthetic documents whose every field value is known in
advance, then score an extraction model against that ground truth.

The first document type is **invoices**: hundreds of thousands of them, across
dozens of business types, billing models (flat, tiered, metered, subscription,
usage-based), jurisdictions, languages, and capture conditions (clean, scanned,
handwritten). The architecture is not invoice-shaped, though — a
document-agnostic **kernel** does the weaving and a **pack** supplies one
document type. Contracts and legal documents are additional packs, not forks.

## Core principles

Five principles are the spine of the project; every part of the system is meant
to serve them, and new features are designed against them.

1. **Realistic — but no PII, and never for fraud.** Generated documents are
   **synthetic**: they look real, so a corpus is worth testing against, but they
   never carry personally identifiable information. Identifying details (addresses,
   phones, tax IDs) are *derived* deterministically at generation and never stored,
   and LLM-generated descriptions are screened for PII and quality (regex gates — a
   flagged description falls back to procedural text) — so there is no PII-shaped
   field to leak. The
   project creates **no identity documents for fraudulent purposes, and no documents
   intended to enable fraudulent activity** — its purpose is producing test data
   with a known ground truth, not passing anything off as genuine.
2. **Provider-agnostic.** LLMs, cloud providers, and infrastructure backends are
   pluggable — storage, run-state, and export are chosen by URI scheme; models
   are a weighted, config-driven provider mix — with no change to calling code.
3. **Locally capable when possible.** Seed data generates documents offline — no
   cloud account, no API key — whenever the document type allows. It is a
   capability each pack *declares* (`ContentMode` / `local_first`), not a blanket
   promise.
4. **High throughput.** Concurrency is enabled on *all* job types — document
   generation and catalogue building alike — coordinated without a broker or a
   leader, so one run scales across a fleet.
5. **Extensible.** Extending generation is simple, at three levels: *within* a
   pack (a business type, billing model, locale, or template is a table/file
   edit); a *new document type* is a new pack, no kernel fork; and *variants of a
   new type* (a bank statement vs a credit-card statement) are an internal axis of
   one pack, sharing a base by composition. See [Extending](#extending) for the
   boundary rule.

The sections below are these principles made concrete.

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
type generates offline.

This is a declared contract, not a footnote — every pack states its mode, so the
kernel can report and gate on it:

```python
>>> get_pack("invoice").content_capability
ContentCapability(mode=<ContentMode.PROCEDURAL: 'procedural'>, ...)
>>> get_pack("invoice").content_capability.local_first
True
```

An `LLM_BACKED` pack additionally implements `LlmContentBuilder` (say what to
generate, take the text back), and `build_catalogue(pack, mix, budget=...)` runs
that one-time offline step through the weighted provider mix — batching the
batch-capable slice through the Anthropic Batch API. The document run itself
still makes no LLM calls either way: it reads the finished catalogue.

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

Extensibility runs at three levels:

- **Within a pack** — a new business type, billing model, locale, sub-category,
  or template is a table entry or a small file in the pack; no kernel change.
- **A new document type** — a new pack implementing `DocumentPack` +
  `GoldenRecord`. Register it in-tree or ship it as `docloom-yourpack` via the
  `docloom.packs` entry-point group.
- **Variants of a document type** — e.g. a `statement` pack producing bank,
  credit-card, and brokerage statements: one base document with per-variant field
  overlays, not three packs. Model these as an internal variant axis of a single
  pack (a base golden record + optional variant fields, shared builders the
  variants *compose*), the same way the invoice pack carries business types and
  archetypes under one registry entry.

Two rules keep this from eroding the design:

- **The kernel stays document-agnostic — its power is its ignorance.** Shared
  logic comes in three tiers: cross-document (seeding, claim/lease, rendering,
  `GoldenRecord`, money, locale, providers) belongs in the **kernel**;
  domain-shared (a statement's account header, transaction ledger, running-balance
  math) belongs in the **pack**, *never* hoisted into core to avoid duplication;
  variant-specific logic is a **variant overlay** in the pack.
- **Composition over a base pack.** Draw a pack boundary where the contract with
  the kernel is uniform (one golden-schema shape, one content mode, one renderer
  family). Split into a second pack only when that contract genuinely diverges —
  and then share code through a **library dependency, not an inherited base
  pack** (has-a, not is-a), extracted only when a second pack actually needs it.

See [DESIGN.md](DESIGN.md) for the full architecture and rationale,
[docs/concurrency.md](docs/concurrency.md) for the sharding + multi-worker model
(how one run scales across a fleet without a broker or a leader), and
[docs/deployment.md](docs/deployment.md) for per-platform wiring (local, GCP,
AWS, Azure) with the URIs to point at.

## Status

Early. Kernel, invoice pack, localisation, PDF rendering, and the local
storage/state/sink layer are built and tested (250 tests). Generation
orchestration runs end to end locally; catalogue generation and cloud adapters
are in progress.
