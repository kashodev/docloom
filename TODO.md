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

- [x] **Per-run manifest (and a split manifest for large runs).** Shipped in
      `core/pipeline/manifest.py`. A run is self-describing from the bucket alone,
      which is how a separate consumer (a Cloud Run app that reads the corpus and
      processes it) discovers and verifies it without a StateStore and without
      walking the bucket.
      - **Split as designed.** A part per unit (`<run>/manifest/unit-000123.json`)
        lists that unit's documents and shards with a sha256 each, written by the
        worker *before* the unit is marked done. A root (`<run>/manifest.json`)
        indexes the parts plus run totals, written only on the COMPLETED
        transition — so its presence is the completion signal, and a partial or
        failed run has parts but no root.
      - **Verified against the real `GcsBlobStore` adapter** (fake-gcs-server):
        a consumer reconstructs the full document list from the manifest alone
        (matches the bucket exactly), deep checksum verify passes, and a tampered
        blob is caught. Refuses to write a root over a missing part. Idempotent —
        gated on the state transition, so written once.
      - **Consumer API:** `read_run_manifest`, `is_complete`, `verify_run`
        (shallow / deep), `enumerate_document_keys`.
      - Export is untouched — it walks `<run>/golden/`; the manifest lives under
        `<run>/manifest/` and `<run>/manifest.json`.

- [ ] **Incremental (pull-while-generating) manifest consumption.** Today the
      contract is pull-on-complete: the root manifest appears only when the run
      finishes, so a consumer cannot start processing a large run's early units
      while later ones are still generating. The per-unit parts already exist and
      are written as each unit completes, so the building block is there — what is
      missing is a *documented in-progress contract*: a way for a consumer to
      enumerate the parts that exist so far, know which are final vs still coming,
      and resume as more appear, without ever mistaking a still-generating run for
      a complete one. Deferred at the requester's call — cannot be tested against
      the real consumer apps for some time, and pull-on-complete is correct in the
      meantime.
      - Likely shape: a consumer lists `<run>/manifest/` for parts and treats the
        root's absence as "more may come"; a lightweight run-state read (or a
        progress marker in the bucket) tells it the expected unit count so it can
        tell "not done yet" from "done, here is everything".

- [ ] **Aggregate root manifest over a multi-slice run.** A single `generate` now
      nests its slices under one folder — `runs/<run>/<slice>/…`, each with its own
      root manifest (shipped in #40) — but there is still no single top-level
      `runs/<run>/manifest.json` describing the whole run. A first attempt at that
      aggregate (branch `feat/run-group-manifest`, PR #41 — **closed unmerged**)
      added a `GroupManifest` + `SliceRef`, a `finalize-run` CLI command, and had
      `deploy.sh` write it. It worked, but *how* it got written was unsatisfying —
      both delivery mechanisms were rejected:
      - **A separate finalize job** (what #41 shipped) spends a whole Cloud Run
        execution — cold start, image pull — on a sub-second read-3-manifests,
        write-1 operation. Wasteful for what it does. (It also hit a `gcloud run
        jobs execute` quirk: `--parallelism` is not a valid execute-time override,
        only `--tasks` is. See the related note below.)
      - **Folding it into the last slice's `generate`** ties the aggregate write to
        that slice's `--wait`, which can block for a very long time on a large run;
        if the operator's terminal drops, the manifest is never written. Rejected
        for the same reason detached dispatch (studio item 9) is wanted.
      - **Investigate further — the wanted shape.** Write the aggregate either from
        a cheap standalone **CLI command** the operator runs *once, locally against
        `gs://`* when the slices are done (seconds, no job, no long `--wait`), **or**
        from a **small script**. Decouple it from any long-running job and from a
        live terminal. Open questions: where the slice list comes from (config vs
        bucket discovery), how the operator/tool knows every slice is complete
        (per-slice roots exist ⇒ complete — that's the building block), and where it
        belongs (a `docloom` subcommand, a `scripts/` helper, or the studio flow).
      - Separately: **`deploy.sh` on `main` passes `--parallelism=1` to `gcloud run
        jobs execute` in `status()` and `export_golden()`** (pre-existing, not from
        #41), which gcloud rejects as an unrecognized argument. Drop it — `--tasks=1`
        already pins a single task. Small standalone fix, unrelated to the manifest.

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

- [x] **Let a run's composition be constrained (locale / company / template /
      condition).** `deploy/gcp/run.example.yaml` let an operator describe slices
      — "10k from one company on one template", "2.5k in French", "2.5k
      handwritten" — and the deploy script validated and displayed them, but
      nothing executed them: the sampler drew from the full weighted roster with
      each company's own locale and template. A slice named `french` produced
      English invoices, correctly computed and useless, with nothing to catch it
      but a query nobody thought to run.
      - **`core/selection.py`** holds the declaration: locales, companies (list
        or "use N"), archetypes (list, "use N", or `all`), business types, a
        condition mix, a wear range, goods receipts. Its vocabulary is exactly
        the YAML block the deploy config already used — inventing a second format
        to execute the first would have been the wrong repair.
      - **`packs/invoice/composition.py`** resolves it against the roster, once
        per run, and *raises* rather than falling back. Constraints compose:
        `locales: [fr-FR]` + `business_types: [telecom]` is French telecom
        issuers, and no match is an error.
      - **"Use N of them" is seeded from the run id**, so a resumed unit draws
        the same pool rather than quietly changing the corpus mid-run.
      - **Surfaced twice**: `docloom generate --locale/--company/--archetype/
        --business-type/--condition/--wear/--goods-receipt`, and
        `--selection-file` taking the same YAML block. One parser behind both.
        `deploy.sh` emits the flags per slice, so a config finally executes.
      - **Currency, tax model and address were never separate knobs** — they
        follow the issuer's jurisdiction, so `locales: [en-GB]` is how a slice
        asks for GBP and UK addresses.
      - Fixed on the way: `default_source()` took no arguments, so the existing
        `--max-line-items` flag was accepted and silently dropped.

- [x] **Wire scan degradation into the run pipeline.** `degrade_pdf` was written,
      tested and never called — so `condition` was recorded on the golden row and
      had no effect on the artefact, and the sampler hardcoded every document to
      `CLEAN` regardless. `PdfRenderer` now realises the condition after
      Chromium: CLEAN passes through untouched (text layer intact, no cost),
      anything else rasterises at 150 dpi, degrades with the record's own seed
      and `wear`, and re-wraps with no text layer, exactly like a real scan.
      Verified end to end — a handwritten run's PDFs extract 0 text characters, a
      clean run's extract 753.

- [x] **A run that leaves failed units no longer exits 0.** It reported the
      failures on stdout and returned success, so a Cloud Run execution, a
      `deploy.sh --wait`, and a CI step all read a broken run as a good one. The
      units were always recoverable with `--resume`; the silence was the problem.

- [x] **Expose the catalogue build on the CLI (`docloom catalogue`).**
      *Shipped: builds a per-company catalogue, runs the quality/PII gates, and
      writes a versioned Parquet artifact whose manifest carries the audit.
      The pool is generated procedurally today — the LLM-backed build lands
      behind this same command and the same gates, and nothing downstream
      changes when it does. Remaining LLM work is tracked in the reasoning-model
      item below.* The
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

- [x] **LLM-backed catalogue build.** `packs/invoice/llm_build.py` drives the
      existing `CatalogueRunner` to write product descriptions (and a co-generated
      price band) behind the same `docloom catalogue` command and the same
      validation/PII gates. Structural fields, the roster and the fallback all
      stay procedural: any slot the LLM cannot fill (failed call, unparseable
      output, rejected description, bad band) keeps its procedural product, so a
      build is always complete and a provider outage degrades to the procedural
      pool. The price band is a sampling input, never a golden number — a bad one
      is dropped in favour of the procedural band. `--providers <file>` on the
      catalogue command; provenance (model mix, fill rate, cost, rounds) goes in
      the manifest. Tested against fake providers (no key); the CLI path tested
      through a mock OpenAI transport.
      - **Two key-gated steps remain, and both need real API keys so they are
        the operator's to run, not automatable here:**
        1. *Live re-verification* — the provider fixes (empty-is-failure,
           `enable_thinking:false`, self-correcting estimate) have only run
           against fakes since the last live smoke. Re-run
           `scripts/smoke_catalogue.py` with thinking disabled and confirm all
           three providers return non-empty text within the cap **before** any
           real build. This is the reasoning-model item below.
        2. *A ~1,000-item pilot* (~$0.05) before the full 300k (~$8–10): audit the
           output by hand, then compare variety against the procedural baseline
           to confirm the LLM is buying the semantic long tail it costs.

- [ ] **The OpenAI-compatible provider does not handle reasoning models.** Found
      by a direct smoke test against the three real endpoints in the deploy
      config (deepseek-v4-flash / qwen3.5-flash / claude-haiku-4-5, 40/40/20) —
      the first time the provider layer ran against live APIs rather than the
      fake HTTP transport the unit tests use. Two of the three misbehaved, and
      the config would have produced a broken catalogue at scale. Raw responses
      confirmed both are "thinking" models returning a `reasoning_content` field:
      - **DeepSeek returned empty content.** With `max_tokens=48` it spent all 48
        completion tokens reasoning (`finish_reason: length`,
        `reasoning_tokens: 48`) and emitted zero content tokens — the answer
        never came. `OpenAICompatibleProvider.complete` reads only
        `choices[0].message.content`, so it got `""`.
      - **Qwen ignored `max_tokens` entirely.** `finish_reason: stop` (not
        truncated) after 2,335 reasoning tokens for a ~24-token answer — a
        ~50–75× output overrun, and the reason the qwen items cost 40–80× the
        deepseek ones.
      - **Fixes, roughly in priority order:**
        1. **An empty / `None` completion must not count as success.**
           `complete` takes `content` verbatim; a blank result flows through the
           runner as `ok`, so 40% of a catalogue could come back empty and the
           run would report clean. This is the load-bearing fix — it converts a
           silent-bad-output failure into a loud one.
        2. **Disable thinking for these models.** DashScope takes
           `enable_thinking: false`; DeepSeek's non-thinking model is a different
           id (`deepseek-chat`). For terse catalogue blurbs there is no reason to
           pay for reasoning at all. Reading `reasoning_content` as a fallback is
           the wrong fix — it captures tokens you never wanted.
        3. **Some endpoints want `max_completion_tokens`, not `max_tokens`** —
           worth checking as the mechanism behind Qwen ignoring the cap.
      - Cross-references the budget item below and the missing `docloom
        catalogue` command above: none of this is reachable today (no LLM-backed
        pack, no CLI), so it blocks nothing yet — but it must be fixed before the
        catalogue path is wired, or the first real run wastes money on garbage.
      - Repro: `scratchpad/smoke_catalogue.py` (unmerged); `--raw` dumps the
        message shape that proved the diagnosis.

- [x] **The budget pre-flight estimate is defeated by output that exceeds
      `max_tokens`.** *(Correction: an earlier version of this entry claimed the
      guard did not enforce on actual spend. It does — `BudgetGuard.add` raises
      once cumulative real spend passes the limit, so per-call damage was always
      bounded. The real hole was narrower and is recorded accurately here.)*
      `estimate_cost` predicted output from `request.max_tokens`; Qwen returned
      2,359 against a cap of 48, making every estimate ~50× low.
      - **Where that actually bit: the batch path.** `_run_batched` checks one
        aggregate estimate for a whole batch and then executes it, so a large
        batch could overshoot badly *before* any actual cost reached `add()`.
        The per-call path was backstopped; the batch path was not.
      - **Fixed** by flooring the estimate with observed output: once a response
        has exceeded `max_tokens`, later estimates assume at least that much.
        Self-correcting after one call and conservative in the direction that
        protects the budget.

- [ ] **Exercise the provider layer against real endpoints in CI (gated).** The
      provider/catalogue code is unit-tested only against a fake httpx transport,
      which is why the two bugs above survived — the fake echoes a well-formed
      `content` and never reasons, ignores `max_tokens`, or returns empty. This
      is the same shape as the wheel/emulator/browser blind spots: the test
      double is more forgiving than production. A key-gated smoke (skipped when
      the three API keys are absent, like the emulator/moto tests) that sends one
      real request per configured provider and asserts non-empty content within
      the token cap would have caught both on the first run. `smoke_catalogue.py`
      is the manual version of exactly this.

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

- [ ] **The test suite never exercises the installed wheel.** Everything runs
      from a source checkout (`PYTHONPATH=src`), where no entry points are
      declared. That hid a bug which broke *every* installed copy of docloom:
      the invoice pack is registered both on import and through its
      `docloom.packs` entry point, and `register_pack` compared by identity, so
      `available_packs()` raised `ValueError` before returning anything. 556
      passing tests said nothing; the Docker build's one-line smoke check caught
      it.
      - The registry is fixed (same pack class registered twice is idempotent;
        two *different* classes on one name still raise), but the blind spot is
        not: any packaging, entry-point or data-file problem is still invisible
        until something installs the wheel.
      - Shape: a CI job that `pip install dist/*.whl` into a clean venv and runs
        a handful of smoke assertions — `available_packs()`, `get_pack`, fonts
        and templates resolving from package data, `docloom --help`. Cheap, and
        it covers the whole class rather than this one instance.

## Concurrency & multi-cloud portability
- [x] **Concurrent cold start races the run plan.** Every worker checked
      `get_run(run_id) is None` and created the run if so. When N tasks start
      simultaneously — which is exactly what Cloud Run Jobs does — two could both
      see "no run" and both call `create_run`, the second resetting units the
      first had already claimed (and on SQLite, dying on a UNIQUE violation).
      - **Fixed by making creation conditional**, the same primitive the unit
        claim already uses: `ON CONFLICT DO NOTHING` (SQLite),
        `document.create()` (Firestore), `attribute_not_exists(pk)` (DynamoDB).
        `create_run` now returns whether *this* caller wrote the plan, so the
        losers wait instead of trampling.
      - **`Run.planned` closes the follow-on window.** Firestore and DynamoDB
        cannot write the marker and the units atomically, so the marker goes down
        first with `planned=False`; a run is claimable only once it flips true.
        Without it a worker reads a marker with no units yet, claims nothing,
        exits 0 and reports a finished run that generated *nothing* — a far
        worse failure than the duplicate work. Absent on older rows ⇒ `True`
        (they were only ever visible fully planned). A planner that dies mid-plan
        is taken over after `PLANNING_TAKEOVER_SECONDS`.
      - **`docloom plan` shipped alongside** — not required (`generate` plans
        safely from every worker), but it lets a large run be planned and
        inspected with `docloom status` before compute is committed to it.
      - **Rejected: worker-ordinal gating** (`CLOUD_RUN_TASK_INDEX` /
        `AWS_BATCH_JOB_ARRAY_INDEX`). It puts coordination in the platform, which
        is backwards — `docs/concurrency.md` is explicit that coordination lives
        in the StateStore — and it does nothing for the local multi-process case.
      - **Found and fixed on the way:** `FirestoreStateStore.create_run` put the
        run doc and *every* unit doc in a single `WriteBatch`, which Firestore
        caps at **500 operations**, so a run of 500+ units failed to plan at all.
        The README's "hundreds of thousands of documents" reaches that at the
        default `unit_size=1000` — 500k documents is 500 unit writes plus the run
        doc, one over. The 25k example config (20 units per slice) never came
        close, which is why it survived. All batch paths (`_write_units`,
        `reset_failed_units`, `reclaim_expired_units`) now chunk.

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
      all implemented; `create_run` is a conditional put that makes exactly one
      of N simultaneous workers the planner (see the cold-start item above).
      Registered in `open_state`
      with `?region=` / `?endpoint_url=`. 27 tests — pure item/sort-key mapping
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
