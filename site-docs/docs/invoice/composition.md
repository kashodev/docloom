# Composition & selection

A run rarely wants "any invoice." **Composition** constrains what a slice may draw
from — a locale, a company, a template, a capture condition — and the invoice pack
**resolves and validates that up front** so a nonsensical corpus fails at config
time, not 40 minutes into a run.

The vocabulary is the kernel's [`Selection`](../core/misc.md); `composition.py`
resolves it against the roster (`resolve`), raising `UnsupportedConstraint` for
anything the pack can't honour.

## The axes

| Axis | Constrains |
|---|---|
| `locales` | Issuer jurisdiction (currency, tax model, language follow it). |
| `companies` | A pinned company id, or "use N" (seeded from the run id). |
| `archetypes` | Which of the 7 [templates](rendering.md). |
| `business_types` | e.g. `retail`, `telecom`, `b2b_saas`, `ai_platform` (`enums.py`). |
| `conditions` | `clean` / `light_scan` / `heavy_scan` / `handwritten`. |
| `wear` | Crisp ↔ worn (a value or a range). |
| `goods_receipt` | Delivery notes with a receiver's signature. |
| `date_range` | The issue-date window. |

"Use N of them" is **seeded from the run id**, so a resumed unit draws the same
pool rather than quietly changing the corpus mid-run. Currency, tax model, and
address are not separate knobs — they follow the issuer's jurisdiction, so
`locales: [en-GB]` is how a slice asks for GBP and UK addresses.

## Realism rules (validated, not silently ignored)

A slice named `french` that emits English is a wasted run nobody notices, so the
pack **refuses** impossible combinations rather than dropping them:

- **Handwritten always renders as the handwritten pad** (`handwritten-form-01`) —
  there is no typeset handwritten archetype.
- **Handwritten ≠ telecom** — a 50-page hand-filled itemised bill is nonsense.
- **Goods-receipt is physical goods only** — a receiver's signature only makes
  sense for a delivery.
- **Born-digital issuers are CLEAN-only.** A software/AI business (`b2b_saas`,
  `ai_platform`) is dropped from any non-clean condition (scan *or* handwritten) —
  too new to have a hand-filled or aged-paper original. Pinning such a type *and* a
  scan/handwritten condition is a hard `UnsupportedConstraint`.
- **Era floor (soft, opt-out).** An issue date is floored up to the issuer type's
  era (e.g. `ai_platform` → 2025) — but this **never blocks a run**: it caps within
  the window, and `--no-date-era-floor` / `enforce_date_era: false` turns it off so
  a run can deliberately produce anachronistic dates.

See [Design decisions → Selection](../design-decisions.md#selection-realism-constraints)
for the *why* behind each.

## Two surfaces, one parser

Composition is expressed twice — as `docloom generate` flags
(`--locale`/`--company`/`--archetype`/`--business-type`/`--condition`/`--wear`/
`--goods-receipt`) and as a `--selection-file` YAML block (the same block a
`deploy.sh` slice uses) — behind one parser, so they cannot drift. The
[studio](../studio/steps.md) prompts a single inline slice or points at a
`run.yaml` for a full multi-slice composition.
