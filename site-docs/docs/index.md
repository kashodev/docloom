# docsynth

Weave templates and generated data into realistic **synthetic documents** — and
get a **computed golden dataset** alongside them, exact to the cent.

!!! info "Primary intended use"
    docsynth generates **synthetic documents to test AI/OCR document-extraction
    pipelines**. It produces a large, varied corpus whose every field value is
    known in advance (the golden dataset), so an extraction model can be scored
    against ground truth. The documents are synthetic — the project creates **no
    identity documents for fraudulent purposes and no documents intended to enable
    fraudulent activity**.

The first document type is **invoices**: hundreds of thousands of them, across
dozens of business types, billing models, jurisdictions, languages, and capture
conditions (clean, scanned, handwritten). The architecture is not invoice-shaped,
though — a document-agnostic **kernel** does the weaving and a **pack** supplies
one document type.

## Core principles

1. **Realistic — but no PII, and never for fraud.** Generated documents are
   synthetic; they never carry PII (identifying details are derived at generation
   and never stored; generated descriptions are screened for PII/quality), and the
   project produces no documents intended to enable fraud.
2. **Provider-agnostic.** LLMs, cloud providers, and infrastructure backends are
   pluggable by URI scheme / config-driven provider mix — no calling-code change.
3. **Locally capable when possible.** Seed/procedural data generates offline (no
   key) whenever the document type allows; a pack *declares* the capability.
4. **High throughput.** Concurrency on all job types (generation + catalogue
   build), coordinated brokerless/leaderless so it scales across a fleet.
5. **Extensible — at three levels.** Within a pack (a table/file edit); a new
   document type (a new pack, no kernel fork); variants of a type (an internal
   axis of one pack, sharing a base by composition).

## Where to go next

- **Architecture → Overview** — the kernel + packs big picture.
- **Invoice pack → Overview** — the whole generate/catalogue/export flow.
- **Studio (the CLI)** — `docsynth studio`, the interactive/scriptable orchestrator.
- **Operations** — the deploy tool and running on GCP.

!!! note "Site status"
    This site is being built out section by section (see the build plan). Pages
    marked as stubs are placeholders until their content lands.
