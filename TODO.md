# TODO

Tracked follow-ups that are deliberately deferred, not forgotten.

## Before / soon after first public push
- [x] **Add a `LICENSE` file.** MIT `LICENSE` added and declared via PEP 639
      (`license = "MIT"`, `license-files = ["LICENSE"]`) with authors, readme,
      keywords, and trove classifiers.
- [x] **Change the copyright holder in the MIT `LICENSE`** to `Adam Okasha`
      (and the matching `authors` in `pyproject.toml`).
- [x] **Add the project logo to the README.** `docloom-logo-page-slate.svg` now
      leads the README (it already reads "docloom", so it replaced the H1).
- [x] **Check the generated `samples/` PDFs into git.** Dropped the stale
      `templates/*.pdf` ignore, kept the blanket `*.pdf` guard, and added a
      scoped `!samples/**/*.pdf` exception; committed 45 synthetic samples
      (confirmed no PII PDFs remain anywhere) with a `samples/README.md`.
- [x] Review the README framing given the repo generates realistic synthetic
      financial documents: make the testing/eval intent unmistakable up front.
      (Intro leads with the golden-dataset-for-scoring-extraction purpose; the
      "local-first" section was rescoped — see below.)

## Content generation strategy per pack
- [x] **Make "local-first" a per-pack property, and give text-heavy packs a
      first-class LLM source.** `core/content.py` formalises the contract:
      `ContentMode` (`procedural` | `llm_backed`) + `ContentCapability` (with
      derived `local_first` / `requires_api_key`), declared by every pack via
      `DocumentPack.content_capability` and read defensively through
      `capability_of` (defaults to procedural for older packs). An LLM-backed
      pack also implements the `LlmContentBuilder` protocol (`catalogue_items` /
      `ingest`), and `build_catalogue(pack, mix, budget=...)` drives that offline
      step through the provider mix + catalogue runner (Batch slice included),
      refusing a procedural pack. InvoicePack declares `PROCEDURAL`; README
      documents the contract.

## Rendering fidelity
- [x] **Bundle OFL fonts for portable typography.** Four keys (serif-classic →
      Noto Serif, sans-neutral → Inter, slab → Zilla Slab, mono-invoice →
      JetBrains Mono) now embed a bundled OFL woff2 (weights 400/700) as base64
      `@font-face` via `fonts.font_face_css`, and `font_stack` leads with the
      embedded family — byte-identical rendering for those typefaces on any host.
      Files + licence in `src/docloom/packs/invoice/fonts/`. Remaining keys still
      resolve from their semantic fallback stack; add more the same way (drop
      woff2 into `fonts/files/`, extend `BUNDLED`, note it in `OFL.txt`).

## Concurrency & multi-cloud portability
- [x] **Lease + reclaim for crashed workers.** Each claim now stamps a
      `lease_expires_at` (default 15 min, configurable per store) and clears it
      on complete/fail/reset. `StateStore.reclaim_expired_units` returns
      `running` units with a lapsed lease to the pool; SQLite also reclaims
      opportunistically inside the claim's write lock (so a draining fleet
      self-heals without a resume), while Firestore reclaims explicitly (a
      per-claim scan is too costly at scale). `resume_run` reclaims on resume.
      Covered by unit tests on both adapters. Follow-up: a lease *renewal*
      heartbeat for units that legitimately run longer than one lease window.
- [x] **DynamoDB StateStore adapter (`dynamodb://`).** Built on a single table
      (`pk` = run id, `sk` = `RUN` / zero-padded `UNIT#…` so an ascending Query
      is index order). The atomic claim is a conditional write (update *only if*
      `state = 'pending'`); a lost race advances to the next candidate. Lease +
      reclaim, failed-unit reset, pause/cancel gating, and paging past a batch
      all implemented; `create_run` writes units first and the run marker last,
      so a crash never leaves a discoverable half-run. Registered in `open_state`
      with `?region=` / `?endpoint_url=`. 14 tests — pure item/sort-key mapping
      plus integration against real boto3 (moto in-process, or DynamoDB Local via
      `DYNAMODB_ENDPOINT`). Postgres/RDS via `SELECT … FOR UPDATE SKIP LOCKED`
      remains the alternative if wanted.

## Documentation
- [x] **Deployment & configuration guide** — [docs/deployment.md](docs/deployment.md).
      Covers local (single box, `sqlite://` + `file://` + `duckdb://`), GCP
      (Cloud Run Jobs + `gs://` + `firestore://` + `bigquery://`, the reference
      stack), AWS (Batch array jobs / ECS-Fargate / EC2 + `s3://` +
      `dynamodb://`), and Azure (Container Apps Jobs / Azure Batch — with the
      missing `az://`, Cosmos/Table-Storage, and Synapse adapters called out
      honestly). Leads with the two rules that drive everything (all workers must
      reach the same StateStore; `sqlite://` is single-box), documents the
      `unit_size` sizing heuristic, carries a status table of which adapters are
      actually built, and flags the spot/preemptible lease caveat.
- [x] **Concurrency & sharding architecture doc.** Written up in
      [docs/concurrency.md](docs/concurrency.md): units = index ranges = shard =
      export granularity; `stable_seed(run_id, index)` determinism; the atomic
      claim as the single (pull-based, broker-less) coordination point; the two
      concurrency levels; pause/resume/cancel via run-state gating; failed-unit
      reset + lease reclaim on resume; large-document routing (planned); and why
      the compute layer is swappable (coordination lives in the StateStore).
      Cross-links the deployment guide (still TODO).

## Docs & landing site (large, do last)
- [ ] **Separately deployable landing page + developer-docs app, in this repo,
      backed by mkdocs-material.** One site that houses *all* the documentation:
      a marketing/landing front page plus the full developer docs (getting
      started, architecture, the pack contract, the concurrency model, the
      deployment guide, API/reference). Requirements to work out when actioned:
      - **In-repo, independently deployable.** Its own subdirectory (e.g.
        `docs-site/`) with its own `mkdocs.yml`, build, and deploy pipeline —
        buildable and shippable without publishing the Python package, and vice
        versa.
      - **mkdocs-material** as the framework (nav, search, theming, versioning
        via `mike` if we want per-release docs).
      - **Single source of truth.** Fold the existing Markdown docs
        (`README.md`, `DESIGN.md`, `docs/concurrency.md`, the deployment guide
        once written) into the site rather than duplicating them; keep authoring
        in Markdown so they stay diffable.
      - **Landing page** distinct from the docs tree (custom `index.html` /
        overrides or a Material "splash" home) — the project pitch, not a doc.
      - Decide hosting/deploy (GitHub Pages via `gh-deploy`, or a static host)
        and wire CI so docs deploy on merge. Do this **last** — it depends on the
        deployment guide and the rest of the docs being settled.

## Backlog
- [x] Cloud adapters end-to-end verification against emulators. GCS
      (`GcsBlobStore`) is verified against **fake-gcs-server** and Firestore
      (`FirestoreStateStore`, including the lease/reclaim lifecycle) against the
      **Firestore emulator** — real SDK calls, env-gated, skipped without the
      emulator. BigQuery has no local emulator (external tables read real GCS),
      so its end-to-end test is gated on a real project
      (`DOCLOOM_BQ_PROJECT/DATASET/STAGING`). Also fixed a deprecated positional
      Firestore `where()` call surfaced by the emulator run.
- [x] **`(cont'd)` markers on page-spanning sections** (telecom archetype). The
      PDF renderer now measures each section table's top/bottom in the print-
      width layout and, for sections whose extent crosses a page boundary, fills
      the `.contd` marker in the (repeating) section header with the localised
      "(cont'd)" / "(suite)". The break geometry is a pure, unit-tested function
      (`spanning_sections`); a Chromium-gated test confirms a real multi-page
      telecom section is flagged and a short invoice is not. Approximate at the
      exact boundary (a `break-inside: avoid` row that hops pages shifts the
      split by a row) — fine for a cosmetic cue. NB: with one repeating `<thead>`
      the marker necessarily shows on every page of a spanning section including
      the first; true per-page differentiation would require renderer-side table
      splitting, not worth the golden-data risk for a cosmetic nicety.
- [x] **Move running-header composition into the pack.** Added
      `DocumentPack.header_fields(record) -> RunningHeader` (kernel dataclass:
      `primary` text + localised `page_label` template). The invoice pack composes
      issuer + invoice number and localises both; the PDF renderer just escapes
      `primary` and swaps Chromium's counters into the page label — no more
      record-shape getattr in the renderer. A second document type supplies its
      own header without touching the renderer.
- [x] Catalogue runner + logo generation. `CatalogueRunner` drives the weighted
      `ProviderMix` over many items under a `BudgetGuard`: deterministic per-item
      routing (stable hash), bounded-concurrency for per-call providers, and the
      **Anthropic Batch API** (half price) for the batch-capable Haiku slice —
      `AnthropicProvider.complete_batch` submits one batch, polls, and restores
      input order via `custom_id`. Isolated per-item/batch failure handling.
      Tested with in-memory fakes (routing, cost, batching, budget stop,
      failures) + a fake batches client. Logo generation shipped earlier as the
      procedural `logos.py` marks. Still open: wiring a concrete catalogue-content
      pack step onto this runner (the LLM-backed source in the content-strategy
      item above).
- [x] Scan-degradation + handwriting variants (post-process rendered PDFs).
      `core/pipeline/degrade.py` realises a `DocumentCondition`: rasterise the
      clean PDF (pypdfium2, self-contained), degrade each page (skew, blur,
      gaussian noise, dust speckle, JPEG artefacts, desaturation for heavy scan;
      procedural ink — signature + PAID stamp — for handwritten), and re-wrap as
      an image-only PDF with no text layer, so OCR must read pixels. Deterministic
      per seed. Verified with samples (clean / light-scan / heavy-scan /
      handwritten). Remaining: wire it into the run pipeline (assign a condition
      distribution in the sampler + a generic per-record hook to invoke it, à la
      `header_fields`) so runs emit degraded artefacts; and render-time
      handwriting *fonts* for true handwritten text (vs. the overlay here).
