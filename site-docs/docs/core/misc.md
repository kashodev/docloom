# Locale / render / money / logging

The cross-cutting kernel services the pipeline and packs read throughout. None is
document-specific; a pack composes them.

## Money (`core/money.py`)

`money`, `pct`, `sum_money` — all `Decimal` with `ROUND_HALF_UP`, **never float**.
This is what "cent-exact" means: totals are computed and reconciled in exact
decimal, carried through the golden record as strings, and stored as decimal128 in
the artifact/Parquet. A record that doesn't reconcile refuses to exist, so a
rounding drift can never reach a document or the golden set (**① realistic**).

## Locale (`core/locale/`)

`Language` / `Locale` / `Currency` enums, a **table-driven `formatting`** module
(number, currency, and date formats per locale), and a `LabelRegistry` (printed
vocabulary per language). The kernel's Jinja filters read `locale`, `currency`,
and `labels` from the render context, so templates never format explicitly and a
new language is a table entry, not code.

## Render & fonts (`core/render/`, `core/fonts/`)

`core/render/environment.py` builds the Jinja environment with the locale-aware
filters and exposes `render_record(pack, record)` =
`render_template(pack.template_root, pack.archetype_for(record), pack.build_context(record))`
— the one call the [renderer](pipeline.md) makes into a pack. `core/fonts/` owns
the semantic font stacks and the **bundled OFL fonts embedded as base64
`@font-face` data-URIs**, so a chosen typeface renders **byte-identically on any
host** (no system-font drift between a laptop and Cloud Run). Packs supply only
font *selection policy*.

## Selection (`core/selection.py`)

`Selection` is the vocabulary for constraining what a run slice may draw from —
locales, companies (a list or "use N"), archetypes, business types, a condition
mix, a wear range, goods receipts, an issue-date window. The kernel **carries** it
but does not interpret it: a pack resolves it against its roster and raises
`UnsupportedConstraint` for anything it can't honour (a `french` slice that emits
English is a wasted run nobody notices). It is surfaced twice — as `docloom
generate` flags and as a `--selection-file` YAML block — behind one parser.

## Logging (`core/logging.py`)

`structlog` configuration: **console** rendering at a terminal, **JSON** to a pipe
or Cloud Run (with Cloud Logging severity/message fields), and `contextvars`
binding so a run id / unit index thread through log lines without passing a logger
everywhere.

## Registry & enums (`core/registry.py`, `core/enums.py`)

`registry.py` discovers packs through the `docloom.packs` **entry-point group**
(`register_pack`, `get_pack`, `available_packs`) — so a third-party pack ships as
`docloom-yourpack` and is found without a code change. `enums.py` holds the kernel
vocabularies every pack shares: `Jurisdiction`, `DocumentCondition`, `RunState`,
`WorkUnitState`.

The two contracts (`DocumentPack`, `GoldenRecord`, `ContentCapability`) live
alongside these and are covered on the
[Kernel ↔ pack contract](../architecture/contract.md) page.
