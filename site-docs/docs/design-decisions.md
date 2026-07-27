# Design decisions

The non-obvious, **current** decisions that shape docloom — the "why" behind the
built system. Each names the [principle](index.md#core-principles) it serves and
links to where it lives. Superseded designs are deliberately absent.

## Content & catalogue

- **Content built once, offline, into a versioned artifact; generation then draws
  from it locally with no key.** *(③ ⑤)* The whole shape of the system — see
  [Architecture → Overview](architecture/overview.md).
- **Cent-exact golden data.** `Decimal` `ROUND_HALF_UP` end to end, decimal128 in
  Parquet — money is never a float, and a record that doesn't reconcile refuses to
  render. *(①)* [money](core/misc.md).
- **The procedural catalogue is both skeleton and fallback.** The LLM overlay
  writes descriptions + a price band and falls back to the procedural product per
  item, so a build is *always complete* and a provider outage degrades rather than
  fails. *(③ ①)* [Catalogue building](invoice/catalogue.md).
- **Per-index seeding (`stable_seed`)** makes a company/range independent and
  reproducible, so a sharded build equals a single-file build. *(④)*
  [Pipeline](core/pipeline.md).
- **A catalogue build *is a run*** over company ranges — it reuses the claim /
  lease / manifest / budget machinery; the root manifest is the completion signal
  and refuses a gap. *(④)* [Catalogue building](invoice/catalogue.md).
- **Identity is never stored** — derived from `company_id` via `derive_identity`
  at generation. *(①)* [Data model](architecture/data-model.md).

## Providers, budget, resilience

- **Deterministic per-item provider routing** by a stable hash → a weighted mix;
  reproducible. *(②)* [Providers](core/providers.md).
- **Circuit breaker.** A provider returning a streak of empty completions *or*
  errors is quarantined; in-flight calls are **cancelled mid-round** (`as_completed`,
  not `gather`) so a *hanging* provider can't burn the task's wall clock; budget
  declines are excluded from the streak. *(④)* [Providers](core/providers.md).
- **Fallback pool.** A quarantined provider's share is redistributed by a configured
  `(target, share)` list; **procedural is a first-class free sink**; quarantine
  **persists across a build's units** (an atomic set-union in the run state) and is
  recorded per shard for audit. *(④)* [Providers](core/providers.md).
- **Fleet-wide budget.** `DistributedBudgetGuard` counts against the store's spend
  rollup; a budget hit **degrades to procedural**, it doesn't fail the unit. *(④)*
  [State stores](core/state.md).
- **Reasoning models are disabled per vendor.** deepseek uses
  `extra_body: {thinking: {type: disabled}}`; qwen/dashscope uses
  `enable_thinking: false` — otherwise they spend the token budget reasoning and
  return an empty (but billed) completion, which is treated as a failure → fallback.
  *(②)* [Providers](core/providers.md).

## Pipeline & coordination

- **Atomic claim + lease + reclaim**, backend-neutral. The Firestore claim does
  **not** scan for expired leases (explicit reclaim on resume / build start — a
  per-claim scan is too costly). *(④ ②)* [Concurrency](architecture/concurrency.md).
- **Planning race via `Run.planned`** — an already-planned re-run resumes quietly;
  cold-starting N tasks yields exactly one planner. *(④)*
  [Concurrency](architecture/concurrency.md).
- **Completion via the root manifest + gap-refusal**; a worker's exit code keys on
  **real holes only**, not on peers still finishing. *(④)*
  [Pipeline](core/pipeline.md).
- **The golden set is two flat tables** (`invoices` + `line_items`), one row per
  printed row, sha256 per shard. *(①)* [Data model](architecture/data-model.md).

## Rendering

- **Jinja → HTML → Chromium PDF**, with `fit_scale` to avoid a near-empty trailing
  page and the running header from the pack. *(①)* [Invoice generation](invoice/generation.md).
- **The degrade pipeline** turns a clean PDF into a scan (rasterise + noise /
  speckle / JPEG, no text layer); **`wear`** drives ink roughening and scan
  degradation *together*, so crisp and worn are one dial. *(①)* [Pipeline](core/pipeline.md).
- **Handwritten** = bundled OFL handwriting faces + per-field jitter + a procedural
  stamp and signature; `condition: handwritten` is inherently degraded. *(①)*
  [Invoice generation](invoice/generation.md).

## Selection (realism constraints)

A slice's [`Selection`](core/misc.md) is resolved and validated **up front**
(`UnsupportedConstraint`) so a nonsensical corpus fails at config time, not 40
minutes into a run.

- **Handwritten always renders as the handwritten pad** (no typeset archetype), and
  **handwritten ≠ telecom** (a 50-page hand-filled invoice is nonsense). *(①)*
- **Born-digital issuers are CLEAN-only.** A software / AI business (`b2b_saas`,
  `ai_platform`) is dropped from any non-clean condition — it is too new to have a
  hand-filled or aged-paper original. Pinning such a type *and* a scan/handwritten
  condition is a hard `UnsupportedConstraint`. *(①)*
- **Era floor — a soft, opt-out default.** An issue date is floored up to the
  issuer type's era (e.g. `ai_platform` → 2025), but this **never blocks a run**:
  it caps within the window and `--no-date-era-floor` / `enforce_date_era: false`
  turns it off, so a run can deliberately produce anachronistic dates. Date-era
  realism is an omittable input, not a hard rule — the operator decides. *(①)*

## Observability

- **structlog**: console at a TTY, **JSON with Cloud Logging `severity`/`message`
  first** in a pipe / Cloud Run; tracebacks carry no frame locals (a failure record
  stays legible); contextvars bind `run_id` / `unit` / `task`. *(④ ②)*
  [logging](core/misc.md).
