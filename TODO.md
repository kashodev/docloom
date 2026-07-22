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

## Features (larger, self-contained work)

- [ ] **Per-run manifest (and a split manifest for large runs).** Every
      generation run should emit a manifest describing what it produced, so a
      consumer can discover and validate a run without walking the bucket. It
      belongs next to the artefacts under the run prefix.
      - **Contents:** run id, pack, config id, created/completed timestamps,
        totals (documents, units, per-table golden rows), the unit -> index-range
        map, storage layout (document and shard key patterns), per-unit counts
        and checksums, the pack/schema version, and the condition/archetype mix
        actually generated.
      - **Splitting:** a run of hundreds of thousands of documents produces a
        manifest too large to be one useful file. Shard it the way the golden
        data is already sharded — a manifest part per unit-range, written into a
        subfolder — with a **main manifest** at the run root that indexes the
        parts (part key, unit range, document count, byte size, checksum) plus
        the run-level totals. One small file to fetch first; the parts only when
        needed.
      - **Why:** it makes a run self-describing for evaluation and transfer,
        gives export something authoritative to reconcile against, and is the
        natural place to record that a run finished cleanly vs. was resumed.
      - Note the storage layout already gives each run its own prefix and buckets
        documents per unit (`<run>/documents/unit-000123/…`), so the manifest
        just needs to describe that structure rather than invent one.

- [x] **A "crisp" (well-preserved) handwritten variant.** Shipped as the `wear`
      dial (0..1) on the record: one value scales the SVG ink displacement at
      render time and the scan degradation afterwards, so the two always agree.
      Neither end reaches zero — a crisp document is still a capture of paper.
      Recorded on the golden row (`wear`, `is_crisp`) as an eval slice, and kept
      a separate axis from legibility. Sample 47. Original note: every handwritten
      invoice looks like an old, worn document: the ink-roughening turbulence and
      the scan degradation both run at full strength. Add a variant where the
      physical artefact is *recent and in good condition* — the paper is not
      deteriorated, the copy is sharp.
      - Stamp and pen ink should still not be vector-clean (a real die and a real
        biro never are), but the bleed/displacement should be scaled back
        markedly rather than switched off.
      - Likely shape: an "ink wear" / "condition" strength scalar (0..1) driving
        the turbulence `scale` and the degradation profile together, so crisp and
        worn are ends of one dial rather than two code paths. Note this composes
        with, but is distinct from, the existing *legibility* dial (which is about
        the writer's hand, not the paper's condition).

- [x] **Goods-receipt variant with a receiver's signature line.** A second
      handwritten layout for the case where a customer signs on receipt of goods:
      alongside the issuer's "authorised signature", a separate **received by**
      block — signature, printed name, and date received — that a different hand
      signs.
      - This variant must always be **physical goods** (never services or
        subscriptions), since a goods-receipt signature only makes sense for a
        delivery. Constrain the sampler's business type / line-item kinds
        accordingly for this variant.
      - The receiver's hand should differ from the issuer's writer (a different
        face and jitter stream), because it is a different person.

- [x] **LLM token-usage and cost telemetry per run.** `core/usage/` records every
      LLM call — model, tokens, exact `Decimal` cost, and what it was generating —
      as its own `llm_usage` table, **on by default** (`--llm-usage`, `off` to
      disable). Pluggable by URI like the rest of the app: `shard://` (default,
      gzipped-JSONL beside the golden data), `firestore://`, `dynamodb://`,
      `null://`.
      - Deliberately *not* golden data: token counts are not reproducible, and the
        golden record's value is that it is exactly recomputable.
      - A replayed unit **overwrites** its rows on every backend (shard key /
        deterministic doc id / deterministic sort key), so retries cannot
        double-count spend.
      - Free when there is nothing to record: a procedural pack issues no calls,
        so it writes no rows and no shard.
      - `export_run` discovers tables by prefix, so it exports to
        Parquet/DuckDB/BigQuery with no export change at all.
      - **Done:** per-(run, model) spend rollup in the StateStore
        (`add_spend` / `spend` / `total_spend`, atomic on all three backends,
        integer nano-dollars so the counter is exact), plus
        `DistributedBudgetGuard`, which makes a budget hold across a fleet rather
        than per process.
      - Remaining: a streaming path, if live fleet-wide cost visibility at finer
        granularity than the rollup is ever needed — see
        `feature_explorations/llm-cost-telemetry.md`. Not needed for throughput
        (~35 rows/s); only for latency, and it would reintroduce the duplicate
        problem the shard keys currently avoid.

- [ ] **Let a run's composition be constrained (locale / company / template /
      condition).** `deploy/gcp/run.example.yaml` lets an operator describe
      slices — "10k from one company on one template", "2.5k in French", "2.5k
      handwritten from a pool of 10 companies" — and the deploy script validates
      and displays them, but nothing can execute them: `docloom generate` has no
      such flags and the sampler draws from the full weighted roster with each
      company's own locale and template.
      - **The seam already exists.** `InvoiceSampler(goods_receipt=True)` filters
        the roster and the product pool; this is the same idea generalised, not
        new machinery.
      - Shape: sampler selection options (locales, company ids or a count,
        archetypes or a count, a condition mix) built as roster/product filters,
        surfaced as `docloom generate --locale … --company … --archetype …
        --condition …`, or as a `--selection-file` taking the same YAML block the
        deploy config already uses rather than inventing a second format.
      - Choosing *N of something* (10 companies, 3 templates) should be seeded
        from the run id so the chosen subset is reproducible.
      - Until then a slice is only a size and a run id; the composition fields
        are documented intent. What was actually produced is queryable —
        `locale`, `company_id`, `condition`, `is_handwritten` are on the golden
        row — so a run can at least be audited after the fact.

- [ ] **Expose the catalogue build on the CLI (`docloom catalogue`).** The
      provider mix, budget guard and `build_catalogue` all exist in
      `core/providers` and `core/content`, but nothing reaches them from the
      command line — so "which LLMs, in what proportion, under what budget" is
      configurable in `deploy/gcp/run.example.yaml`, validated by the deploy
      script, and then not executable.
      - Shape: `docloom catalogue --pack contract --providers <spec> --budget 50
        --state … --usage …`, building the mix via the existing
        `providers.factory.build_mix` and running `content.build_catalogue`.
      - Needs a way to express the mix on the command line or from a config file;
        the deploy script already parses it from YAML, so a `--providers-file`
        taking the same block would avoid inventing a second format.
      - Until it exists, an LLM-backed pack cannot be run end to end, and the
        `catalogue:` block in the deploy config is documentation rather than
        configuration.

- [ ] **Investigate logging and metrics.** The project has `structlog` as a
      dependency but no deliberate logging or metrics story: what a run should
      emit, at what level, in what format, and where it goes on each platform.
      Worth covering when investigated — structured vs human-readable output and
      how they coexist in a CLI; per-worker vs per-run context (run id, unit
      index) threaded through without passing a logger everywhere; which
      operational metrics matter (documents/sec, unit duration, render failures,
      claim contention, lease reclaims) and whether they belong in logs, a
      metrics endpoint, or the existing run state; how it lands on Cloud
      Run/Batch/local; and what *not* to log (document content — the corpus is
      synthetic, but the habit should hold if a pack ever handles real input).
      Distinct from the LLM cost telemetry already built, which is analytics
      data, not operational signal — though the two should agree on how a run is
      identified. Investigation first, no implementation.

- [ ] **Stroke-level handwriting synthesis (top realism tier, optional extra).**
      The font-based approach (bundled OFL handwriting faces + per-field jitter)
      gets a convincing *filled-in form*, but every occurrence of a letter is the
      same glyph — a trained eye, and a discriminative model, can detect the
      reuse. Genuine variation needs **stroke-level synthesis**: a model that
      emits pen trajectories for arbitrary text, so the same word differs every
      time it is written.
      - **Approach:** Graves-style RNN handwriting synthesis (the classic
        `handwriting-synthesis` LSTM trained on IAM-OnDB) or a modern diffusion
        HTR generator. Both take (text, style vector) -> a sequence of pen
        points with pen-up/pen-down, which we render as variable-width strokes.
      - **Why it preserves the golden data:** the model renders *the exact text
        we give it*, so the computed values are unchanged — this is the crucial
        difference from generating the whole document with an image model, which
        would invent digits and break the cent-exact ground truth.
      - **Per-writer consistency:** a style vector per catalogue company gives
        the same "person" a consistent hand across their invoices, while each
        instance still varies — the realism property fonts cannot provide.
      - **Packaging:** ship behind an optional `docloom[handwriting]` extra
        (PyTorch or, better, an exported ONNX model for a light CPU runtime).
        Keep it out of core: it is a heavy dependency and the font path must
        remain the key-free, local-first default. Determinism via a seeded
        generator, as everywhere else.
      - **Cost:** materially slower per document than fonts, so likely applied
        to a *slice* of the corpus rather than every handwritten document.
      - Depends on the `handwritten-form` archetype (built) supplying the field
        geometry the strokes get drawn into.

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
- [x] **Move the font bundle into the kernel.** `docloom.core.fonts` now owns the
      woff2 files, the semantic stacks and the base64 `@font-face` embedding;
      packs keep only selection *policy* (which faces they draw from). A second
      pack gets byte-identical typography without duplicating the bundle. Also
      fixed a latent packaging bug it surfaced: the `force-include` table was
      duplicating every font and template, so `pip wheel .` failed outright — a
      test now pins the table empty.
- [x] **Company stamps on handwritten documents.** Replaced the word-in-a-box
      mark with a procedural SVG seal carrying the issuer's registered name, town
      and registration number — circular / oval / rectangular, in desaturated
      red/blue/violet pad ink, roughened by a turbulence filter. Placement is
      drawn from six plausible zones with jitter and a hand-pressed angle, never a
      corner or dead centre, and the die is keyed to the company so one office
      keeps one stamp across its invoices.
- [x] **Bundle OFL fonts for portable typography.** Four keys (serif-classic →
      Noto Serif, sans-neutral → Inter, slab → Zilla Slab, mono-invoice →
      JetBrains Mono) now embed a bundled OFL woff2 (weights 400/700) as base64
      `@font-face` via `fonts.font_face_css`, and `font_stack` leads with the
      embedded family — byte-identical rendering for those typefaces on any host.
      Files + licence in `src/docloom/core/fonts/`. Remaining keys still
      resolve from their semantic fallback stack; add more the same way (drop
      woff2 into `fonts/files/`, extend `BUNDLED`, note it in `OFL.txt`).

## Concurrency & multi-cloud portability
- [ ] **Concurrent cold start races the run plan.** Every worker checks
      `get_run(run_id) is None` and creates the run if so. When N tasks start
      simultaneously — which is exactly what Cloud Run Jobs does — two can both
      see "no run" and both call `create_run`, the second resetting units the
      first had already claimed.
      - **Bounded, not benign-by-luck:** generation is deterministic per
        `(run_id, index)` and every blob write is an idempotent overwrite, so the
        result is *duplicated work*, not corrupted output. Worth fixing for the
        wasted compute, not for correctness.
      - **Fix options:** a `docloom plan` command that creates the run and exits
        (run once, then scale out); or gate creation on a worker-ordinal env var
        (`CLOUD_RUN_TASK_INDEX` / `AWS_BATCH_JOB_ARRAY_INDEX`) so only ordinal 0
        plans; or make `create_run` conditional — insert-if-absent — which SQLite
        already gets from its primary key and DynamoDB could get from a
        `ConditionExpression`, but Firestore's `batch.set` would need
        `create()` semantics.
      - Documented as a sharp edge in `docs/deploy-gcp.md` with a two-step
        workaround.

- [ ] **DynamoDB `add_spend` writes two rows non-atomically.** The spend rollup
      increments a `(run, model)` row and the `(run, "*")` total. SQLite does both
      in one `BEGIN IMMEDIATE` transaction and Firestore in one batch, but
      DynamoDB issues two `UpdateItem` calls — each atomic on its own, **not
      atomic as a pair**. A crash, throttle or network failure between them leaves
      the per-model rows summing to slightly less than the total.
      - **Mitigated by write order, not fixed.** The total is incremented
        **first**, so an interrupted pair over-counts the total relative to the
        per-model rows: a budget then stops slightly *early*. The reverse order
        would under-count the total and let a run quietly overshoot its cap —
        the exact failure a budget exists to prevent. Ordering makes the failure
        safe; it does not make the two rows consistent.
      - **Fix:** `TransactWriteItems` over the two items — DynamoDB supports up to
        100, so two is comfortable. Costs roughly 2× the write units of a plain
        update, which is the reason to decide deliberately rather than by default.
      - **Alternative:** drop the per-model rows and derive them from the
        `llm_usage` fact table at query time, leaving only the total in the state
        store. Fewer writes and no divergence possible, at the cost of losing the
        *live* per-model view during a run.
      - Worth a reconciliation check either way: per-model rows should sum to the
        total, and a mismatch is a useful signal that something died mid-write.
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
      `header_fields`) so runs emit degraded artefacts. Render-time handwriting
      is now done — see below.
- [x] **Render-time handwriting (the `handwritten-form-01` archetype).** Replaces
      the old PIL ink overlays, which drew a periodic sine-wave "signature" and a
      crisp-bordered stamp in PIL's tiny default bitmap font — both read as vector
      art, and the line items were still typeset. Now a pre-printed pad with
      ruled item lines: every value is written in one of four bundled OFL
      handwriting faces with per-field jitter, signed in a script face and
      stamped, all roughened by an SVG turbulence/displacement filter so edges
      read as ink. `DocumentCondition.HANDWRITTEN` routes to the archetype, and
      `degrade.py` now only degrades — the ink is already on the paper. Face
      choice is a legibility dial for OCR/HTR difficulty. Golden rows are
      unchanged vs the clean twin (asserted by test).
