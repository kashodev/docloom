# docsynth

Synthetic documents with a **computed golden dataset** — for testing AI/OCR
document-extraction pipelines against a known ground truth.

docsynth generates realistic but synthetic documents whose every field value is
known in advance (exact to the cent). Generate a large, varied corpus, run your
extraction model over it, and score the results against the golden set with a
single SQL join. The first document type is invoices; the architecture is
document-agnostic.

## What you get

- **Cent-exact golden dataset** — ground truth for every document; evaluation is
  one join, identical against local DuckDB or BigQuery.
- **Invoice pack** — invoices across business types, billing models (flat,
  tiered, metered, subscription, usage-based), jurisdictions, languages, and
  capture conditions (clean, scanned, handwritten).
- **PDF / HTML rendering** with realistic capture conditions — scan degradation
  and render-time handwriting for OCR/HTR difficulty.
- **Local-first, cloud-optional** — invoices generate fully offline (no account,
  no keys); point storage / run-state / export at cloud URIs and install the
  matching extra to scale to GCP Cloud Run Jobs.
- **`docsynth studio`** — an interactive/scriptable CLI over the three steps:
  catalog → pdfs → export.
- **Document-agnostic kernel + packs** — a new document type is a new pack, not
  a fork; ship it as `docsynth-yourpack` via the `docsynth.packs` entry point.

## Quickstart

```bash
pip install 'docsynth[studio]'
playwright install chromium      # one-time, for PDF rendering
docsynth studio                  # interactive wizard — pick the local target
```

Requires **Python 3.12+**. Full walk-through: **[Quickstart](https://kashodev.github.io/docsynth/quickstart/)**.

## Core principles

1. **Realistic, but no PII and never for fraud** — synthetic documents that look
   real; identifying details are derived deterministically and never stored, LLM
   text is screened for PII, and no identity or fraud-enabling documents are
   produced.
2. **Provider-agnostic** — LLMs, clouds, and infrastructure backends are
   pluggable (by URI scheme and config-driven provider mix) with no change to
   calling code.
3. **Locally capable when possible** — offline generation wherever the pack
   allows; a capability each pack declares, not a blanket promise.
4. **High throughput** — brokerless, leaderless concurrency across all job types,
   so one run scales across a fleet.
5. **Extensible** — extend within a pack (a table/file edit), add a new document
   type (a new pack), or add variants of a type by composition.

## Documentation

The **[documentation site](https://kashodev.github.io/docsynth/)** (built from
[`site-docs/`](site-docs/)) covers the architecture and rationale, the golden
set, the concurrency/coordination model, per-platform deployment, and the studio.

## Status

Early (`0.1.0`). Kernel, invoice pack, localisation, PDF rendering, the studio,
and the local storage/state/export layer are built and tested (800+ tests).
Cloud adapters and additional packs are in progress.
