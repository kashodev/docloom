"""docloom command line — generate and export runs.

Local-first defaults: with no flags a run writes documents and golden shards
under ``./out`` using ``file://`` storage, a ``sqlite://`` run store, and the
seed catalogue — no cloud, no API keys. Point the URIs at ``gs://`` /
``firestore://`` / ``bigquery://`` (with the matching extra installed) to scale;
nothing else changes.

    docloom generate --run-id r1 --total 1000
    docloom export   --run-id r1 --sink duckdb:///out/golden.db
    docloom status   --run-id r1

Concurrency is by running ``generate`` again — in another process, on another
machine — against the same ``--state``: the atomic claim keeps workers from
colliding. ``--resume`` re-queues failed units first.
"""

from __future__ import annotations

from pathlib import Path

import typer

import docloom.packs  # noqa: F401  — registers the built-in packs
from docloom.core import RunState, WorkUnitState, available_packs, get_pack
from docloom.core.logging import get_logger
from docloom.core.pipeline import (
    HtmlRenderer,
    PdfRenderer,
    create_run,
    export_run,
    resume_run,
    work_run,
)
from docloom.core.selection import Selection
from docloom.core.sinks import open_sink
from docloom.core.state import open_state
from docloom.core.storage import open_store
from docloom.core.usage import DEFAULT_USAGE_URI, open_usage_sink

_log = get_logger(__name__)
app = typer.Typer(add_completion=False, no_args_is_help=False,
                  help="Generate synthetic documents with a golden dataset.")


@app.callback(invoke_without_command=True)
def _init(ctx: typer.Context) -> None:
    """Configure logging before any command runs. Console at a terminal, JSON to
    a pipe/Cloud Run; DOCLOOM_LOG_LEVEL / DOCLOOM_LOG_FORMAT override.

    A bare ``docloom`` in a terminal launches the studio wizard; piped (no TTY) it
    prints help, the conventional no-args behaviour for a script."""
    from docloom.core.logging import configure
    configure()
    if ctx.invoked_subcommand is not None:
        return
    from docloom.studio.prompts import is_interactive
    if is_interactive():
        _run_studio()
    else:
        typer.echo(ctx.get_help())

_STORAGE = typer.Option("./out/blobs", envvar="DOCLOOM_STORAGE", help="Blob store URI for documents + shards")
_STATE = typer.Option("./out/runs.db", envvar="DOCLOOM_STATE", help="Run-state store URI")
# On by default: a run records what its LLM calls cost unless told not to. A
# procedural pack makes no calls, so this is free for invoices; `--llm-usage off`
# disables it outright.
_USAGE = typer.Option(
    DEFAULT_USAGE_URI,
    envvar="DOCLOOM_LLM_USAGE",
    help="LLM usage telemetry URI: shard:// (default) | firestore://… | dynamodb://… | off",
)


# ── Composition flags ───────────────────────────────────────────────────────
# One flag per axis of `Selection`, plus `--selection-file` taking the same YAML
# block `deploy/gcp/run.example.yaml` already uses for a slice. Two surfaces for
# one thing is a deliberate trade: flags are what a deploy script can compose
# programmatically, a file is what a human can read and diff. They share a parser
# so they cannot drift.
_LOCALE = typer.Option([], "--locale", help="Restrict issuers to this locale (repeatable)")
_COMPANY = typer.Option([], "--company", help="Pin to this company id (repeatable)")
_COMPANY_COUNT = typer.Option(0, "--company-count", help="Use N companies, chosen from the run id")
_ARCHETYPE = typer.Option([], "--archetype", help="Restrict to this template (repeatable)")
_ARCHETYPE_COUNT = typer.Option(0, "--archetype-count", help="Use N templates")
_BUSINESS_TYPE = typer.Option([], "--business-type", help="Restrict issuers to this business type")
_CONDITION = typer.Option([], "--condition", help="Capture condition to draw from (repeatable)")
_WEAR = typer.Option("", "--wear", help="crisp | varied | worn | 0.4 | 0.2:0.8")
_GOODS_RECEIPT = typer.Option(False, "--goods-receipt", help="Delivery notes with a receiver's signature")
_SELECTION_FILE = typer.Option("", "--selection-file", help="YAML file holding one slice's composition")
_ISSUE_DATE_FROM = typer.Option("", "--issue-date-from",
                                help="Earliest issue date YYYY-MM-DD (with --issue-date-to)")
_ISSUE_DATE_TO = typer.Option("", "--issue-date-to",
                              help="Latest issue date YYYY-MM-DD (with --issue-date-from)")
_DATE_ERA = typer.Option(None, "--date-era-floor/--no-date-era-floor",
                         help="Floor issue dates to each business type's era (default on); "
                              "--no-date-era-floor allows anachronistic dates")
# A published content artifact. Not required — a pack's built-in pool keeps the
# default path key-free and offline — but it is how a run gets the large, varied
# corpus without shipping hundreds of megabytes in the wheel.
_CATALOGUE = typer.Option("", "--catalogue", envvar="DOCLOOM_CATALOGUE",
                          help="Content catalogue artifact URI (file:// | gs:// | s3://)")


def _parse_wear(raw: str) -> object:
    """``0.2:0.8`` is a range; anything else falls through to the parser, which
    already understands a bare number and the named presets."""
    if ":" in raw:
        low, high = raw.split(":", 1)
        return [float(low), float(high)]
    return raw


def _selection_from(selection_file: str, **flags: object) -> Selection:
    """A selection from a file, a set of flags, or both — flags win.

    Layering rather than either/or: a deploy script can keep the shared slice
    file under version control and still override one axis for a smoke test,
    which is the same reason `deploy.sh` has `--set`.
    """
    import yaml

    base: dict[str, object] = {}
    if selection_file:
        path = Path(selection_file)
        if not path.is_file():
            raise typer.BadParameter(f"no such selection file: {selection_file}")
        base = yaml.safe_load(path.read_text()) or {}
        if not isinstance(base, dict):
            raise typer.BadParameter(f"{selection_file} must hold a mapping, not {type(base).__name__}")

    overrides: dict[str, object] = {}
    if flags["locale"]:
        overrides["locales"] = flags["locale"]
    if flags["company"]:
        overrides["companies"] = flags["company"]
    if flags["company_count"]:
        overrides["companies"] = flags["company_count"]
    if flags["archetype"]:
        overrides["archetypes"] = flags["archetype"]
    if flags["archetype_count"]:
        overrides["archetypes"] = flags["archetype_count"]
    if flags["business_type"]:
        overrides["business_types"] = flags["business_type"]
    if flags["condition"]:
        overrides["conditions"] = flags["condition"]
    if flags["wear"]:
        overrides["wear"] = _parse_wear(str(flags["wear"]))
    if flags["goods_receipt"]:
        overrides["goods_receipt"] = True
    if flags.get("issue_date_from") or flags.get("issue_date_to"):
        if not (flags.get("issue_date_from") and flags.get("issue_date_to")):
            raise typer.BadParameter(
                "give both --issue-date-from and --issue-date-to, or neither")
        overrides["date_range"] = [flags["issue_date_from"], flags["issue_date_to"]]
    if flags.get("date_era_floor") is not None:      # tri-state: unset leaves the file's value
        overrides["enforce_date_era"] = flags["date_era_floor"]

    try:
        return Selection.from_mapping({**base, **overrides})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def generate(
    run_id: str = typer.Option(..., help="Run identifier; seeds all documents"),
    total: int = typer.Option(..., help="Number of documents to generate"),
    pack: str = typer.Option("invoice", help="Document pack"),
    unit_size: int = typer.Option(1000, help="Documents per work unit / shard"),
    fmt: str = typer.Option("pdf", "--format", help="pdf | html"),
    config_id: str = typer.Option("default", help="Run config id (recorded on the run)"),
    max_line_items: int = typer.Option(8000, help="Hard cap on line items per document"),
    resume: bool = typer.Option(False, help="Resume: re-queue failed units, then continue"),
    storage_prefix: str = typer.Option(
        "", "--storage-prefix",
        help="Sub-path under storage for this run's blobs (default: the run id). Set to "
             "<parent>/<name> to nest several runs under one folder."),
    storage: str = _STORAGE,
    state: str = _STATE,
    llm_usage: str = _USAGE,
    locale: list[str] = _LOCALE,
    company: list[str] = _COMPANY,
    company_count: int = _COMPANY_COUNT,
    archetype: list[str] = _ARCHETYPE,
    archetype_count: int = _ARCHETYPE_COUNT,
    business_type: list[str] = _BUSINESS_TYPE,
    condition: list[str] = _CONDITION,
    wear: str = _WEAR,
    goods_receipt: bool = _GOODS_RECEIPT,
    selection_file: str = _SELECTION_FILE,
    issue_date_from: str = _ISSUE_DATE_FROM,
    issue_date_to: str = _ISSUE_DATE_TO,
    date_era_floor: bool | None = _DATE_ERA,
    catalogue: str = _CATALOGUE,
) -> None:
    """Generate documents and golden shards for a run."""
    if pack not in available_packs():
        raise typer.BadParameter(f"unknown pack {pack!r}; available: {', '.join(available_packs())}")

    selection = _selection_from(
        selection_file, locale=locale, company=company, company_count=company_count,
        archetype=archetype, archetype_count=archetype_count, business_type=business_type,
        condition=condition, wear=wear, goods_receipt=goods_receipt,
        issue_date_from=issue_date_from, issue_date_to=issue_date_to,
        date_era_floor=date_era_floor,
    )
    doc_pack = get_pack(pack)
    blob = open_store(storage)
    store = open_state(state)
    usage = open_usage_sink(llm_usage, blob=blob, run_id=run_id)
    source = doc_pack.default_source(selection=selection, max_line_items=max_line_items,
                                     catalogue=catalogue or None)
    version = getattr(source, "catalogue_version", None) or getattr(
        getattr(source, "_catalogue", None), "version", "")
    if catalogue:
        typer.echo(f"catalogue: {catalogue} ({version})")
    if not selection.is_empty:
        typer.echo(f"composition: {selection.describe()}")
    _log.info("generate", run_id=run_id, pack=pack, total=total, unit_size=unit_size,
              fmt=fmt, storage=storage, state=state, catalogue=catalogue or "seed",
              selection=selection.describe())
    renderer = PdfRenderer(doc_pack) if fmt == "pdf" else HtmlRenderer(doc_pack)

    if resume:
        requeued = resume_run(store, run_id)
        typer.echo(f"resume: re-queued {requeued} failed unit(s)")
    else:
        # Safe to call from every worker at once: creation is a conditional write
        # in the store, so one plans and the rest wait for that plan to land.
        create_run(store, run_id=run_id, pack=pack, config_id=config_id,
                   total=total, unit_size=unit_size)

    try:
        stats = work_run(store, run_id=run_id, source=source, renderer=renderer, blob=blob,
                         storage_prefix=storage_prefix)
    finally:
        if isinstance(renderer, PdfRenderer):
            renderer.close()
        # Flush whatever the run's LLM calls recorded. A procedural pack records
        # nothing, so this writes no shard rather than an empty one.
        usage.close()

    typer.echo(
        f"done: {stats.units_completed} unit(s), {stats.documents_written} document(s)"
        + (f", {stats.units_failed} failed" if stats.units_failed else "")
    )
    for unit_index, error in stats.failures:
        typer.echo(f"  unit {unit_index} failed: {error}")
    _print_status(store, run_id)

    # Exit on the *run's* failed count, not this worker's. A worker that failed
    # every unit exits 1, then Cloud Run retries the task — but a fresh
    # (non-resume) worker finds those units already FAILED and out of the
    # claimable pool, so it claims nothing, records no failures of its own, and
    # would exit 0. That is a green execution over a run that produced zero
    # documents — exactly the false pass a whole smoke run hid behind. The run
    # state still knows the truth, so read it: any FAILED unit means the run is
    # not done, and every attempt should say so until `--resume` clears them.
    if store.progress(run_id)[WorkUnitState.FAILED]:
        raise typer.Exit(1)


@app.command()
def plan(
    run_id: str = typer.Option(..., help="Run identifier; seeds all documents"),
    total: int = typer.Option(..., help="Number of documents to generate"),
    pack: str = typer.Option("invoice", help="Document pack"),
    unit_size: int = typer.Option(1000, help="Documents per work unit / shard"),
    config_id: str = typer.Option("default", help="Run config id (recorded on the run)"),
    state: str = _STATE,
) -> None:
    """Create a run's plan without generating anything.

    Not required — `generate` plans the run itself, and does so safely from many
    workers at once. This exists so a large run can be planned and inspected
    (`docloom status`) before committing compute to it.
    """
    if pack not in available_packs():
        raise typer.BadParameter(f"unknown pack {pack!r}; available: {', '.join(available_packs())}")
    store = open_state(state)
    existing = store.get_run(run_id)
    if existing is not None and existing.planned:
        typer.echo(f"run {run_id} already planned: {existing.total_units} unit(s)")
        _print_status(store, run_id)
        return
    run = create_run(store, run_id=run_id, pack=pack, config_id=config_id,
                     total=total, unit_size=unit_size)
    typer.echo(f"planned run {run_id}: {total} document(s) in {run.total_units} unit(s) "
               f"of {unit_size}")
    _print_status(store, run_id)


def _llm_build(providers: str, *, companies: int, products_per_company: int,
               seed: int, budget_usd: float, concurrency: int, use_batch: bool):
    """Load a provider mix and build descriptions with it.

    ``providers`` is either a file path or the mix YAML inline. Inline is what
    lets a Cloud Run job carry the mix in an env var (`DOCLOOM_PROVIDERS`) — the
    mix is configuration, not secret, so it travels as config while the API keys
    come from Secret Manager. Same block shape either way, and the same one the
    deploy config uses, so there is one vocabulary for "which models, in what
    proportion".
    """
    import yaml

    from docloom.core.providers.budget import BudgetGuard
    from docloom.core.providers.factory import build_mix
    from docloom.packs.invoice.llm_build import build_llm_catalogue_sync

    # `providers` is a file path or the mix inline (from DOCLOOM_PROVIDERS on
    # Cloud Run). `Path.is_file()` calls os.stat, which raises ENAMETOOLONG on an
    # inline JSON string rather than returning False — so guard it, and treat any
    # unstattable value as inline content.
    path = Path(providers)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    raw = path.read_text() if is_file else providers
    try:
        config = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"providers is neither a file nor valid YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise typer.BadParameter("providers must be a mapping with a `providers:` list")
    # Accept the deploy config's `catalogue:` block verbatim. `build_mix` wants the
    # list under `text`; the deploy config calls it `providers`.
    if "catalogue" in config:
        config = config["catalogue"]
    specs = config.get("text") or config.get("providers")
    if not specs:
        raise typer.BadParameter(
            "providers needs a `providers:` (or `text:`) list of "
            "{name, model, weight}; see deploy/gcp/run.example.yaml"
        )
    try:
        mix = build_mix({"text": specs})
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(f"bad provider mix: {exc}") from exc

    # A budget from the flag, or from the config's own `budget_usd`.
    limit = budget_usd or float(config.get("budget_usd", 0) or 0)
    budget = BudgetGuard(_dec(limit)) if limit > 0 else None
    return build_llm_catalogue_sync(
        mix, companies=companies, products_per_company=products_per_company,
        seed=seed, budget=budget, concurrency=concurrency, use_batch=use_batch,
        progress=lambda msg: typer.echo(f"  {msg}"),
    )


def _dec(value: float):
    from decimal import Decimal
    return Decimal(str(value))


def _sharded_catalogue(*, out: str, version: str, companies: int, products_per_company: int,
                       seed: int, unit_size: int, build_id: str, state_uri: str,
                       providers: str, budget_usd: float, concurrency: int) -> None:
    """A sharded, resumable build coordinated through a StateStore. Safe to run
    from many tasks at once — the atomic claim splits the company units between
    them, and whoever finishes last writes the root manifest."""
    from docloom.core.state import open_state
    from docloom.packs.invoice.build_run import build_catalogue_run

    mix = None
    provenance: dict = {"generator": "procedural"}
    if providers:
        mix, prov = _resolve_mix(providers)
        provenance = {"generator": "llm", **prov}

    store = open_state(state_uri)
    typer.echo(f"sharded build {build_id} → {out}: {companies:,} companies, "
               f"{unit_size}/shard, {'LLM' if mix else 'procedural'}")
    stats = build_catalogue_run(
        store, out=out, build_id=build_id, catalogue_version=version,
        companies=companies, products_per_company=products_per_company,
        unit_size=unit_size, seed=seed, mix=mix, budget_usd=budget_usd or None,
        concurrency=concurrency, provenance=provenance,
    )
    typer.echo(f"  this worker: {stats.units_completed} unit(s), {stats.products:,} products"
               + (f", {stats.units_failed} failed" if stats.units_failed else "")
               + (f", cost ${stats.total_cost}" if stats.total_cost else ""))
    # Exit non-zero only for a real hole — a unit this worker failed, or a FAILED
    # unit anywhere in the build — because that is what needs a re-run. A worker
    # that finished its share cleanly while peers are still mid-unit is NOT a
    # failure: the build simply isn't globally done yet, and making that early
    # finisher exit non-zero just gets it pointlessly retried. A worker that
    # claimed nothing over a build that already has holes still exits non-zero
    # (build_has_failures catches it), so a broken build never reads green.
    if stats.units_failed or stats.build_has_failures:
        typer.echo("  build has failed units — re-run to retry them", err=True)
        raise typer.Exit(1)


def _resolve_mix(providers: str):
    """Build a ProviderMix from a file or inline YAML, returning (mix, provenance
    hint). Shared by the single-file and sharded LLM paths."""
    import yaml

    from docloom.core.providers.factory import build_mix

    path = Path(providers)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    config = yaml.safe_load(path.read_text() if is_file else providers) or {}
    if "catalogue" in config:
        config = config["catalogue"]
    specs = config.get("text") or config.get("providers")
    if not specs:
        raise typer.BadParameter("providers needs a `providers:` (or `text:`) list")
    fallback = config.get("fallback")
    provenance = {"models": [f"{s['name']}:{s.get('model')}" for s in specs]}
    if fallback:
        provenance["fallback"] = [f"{f['name']}:{f['share']}" for f in fallback]
    return build_mix({"text": specs, "fallback": fallback}), provenance


@app.command()
def catalogue(
    out: str = typer.Option(..., "--out", help="Where to write the artifact (file:// | gs:// | s3://)"),
    version: str = typer.Option(..., "--version", help="Catalogue version, recorded on every golden row"),
    companies: int = typer.Option(1000, help="How many issuers"),
    products_per_company: int = typer.Option(300, help="SKUs each issuer sells"),
    seed: int = typer.Option(0, help="Build seed; the same seed rebuilds the same catalogue"),
    pack: str = typer.Option("invoice", help="Document pack"),
    providers: str = typer.Option(
        "", "--providers", envvar="DOCLOOM_PROVIDERS",
        help="Provider-mix file OR inline YAML (env DOCLOOM_PROVIDERS) → build "
        "descriptions with an LLM. Omit for the procedural (key-free) build."
    ),
    budget_usd: float = typer.Option(0.0, help="Hard USD ceiling for an LLM build (0 = none)"),
    concurrency: int = typer.Option(8, help="Concurrent LLM calls in flight for an LLM build"),
    batch: bool = typer.Option(
        False, "--batch/--no-batch",
        help="Use each provider's batch API (half price on Anthropic, but polls "
        "to completion). Default off: concurrent synchronous calls, predictable "
        "for a bounded job."
    ),
    state: str = typer.Option(
        "", "--state", envvar="DOCLOOM_STATE",
        help="Coordination store URI (sqlite:// | firestore:// | dynamodb://) → a "
        "SHARDED, resumable build that many tasks work concurrently. Omit for a "
        "single-process in-memory build."
    ),
    unit_size: int = typer.Option(200, help="Companies per shard (sharded build)"),
    build_id: str = typer.Option("", help="Build run id (sharded build; defaults to the version)"),
    max_rejection_rate: float = typer.Option(
        0.02, help="Fail the build if more than this fraction of items is rejected"
    ),
) -> None:
    """Build a content catalogue artifact.

    Content is generated **once, offline**, and published; generation then draws
    from it with no API key. That split is what lets a corpus have hundreds of
    thousands of distinct descriptions while `docloom generate` stays local-first
    and deterministic.

    With no `--providers`, the pool is built **procedurally** — combinatorial
    expansion, no keys, no spend. With a provider mix, an LLM writes the
    descriptions (and suggests a price band), falling back to the procedural
    product for anything it cannot fill — so a build is always complete, and a
    provider outage degrades to the procedural pool rather than failing. Both
    paths run the same validation gates and write the identical artifact format.
    """
    if pack != "invoice":
        raise typer.BadParameter(f"no catalogue builder for pack {pack!r}")

    if state:
        _sharded_catalogue(
            out=out, version=version, companies=companies,
            products_per_company=products_per_company, seed=seed, unit_size=unit_size,
            build_id=build_id or version, state_uri=state, providers=providers,
            budget_usd=budget_usd, concurrency=concurrency,
        )
        return

    from docloom.packs.invoice.artifact import write_catalogue
    from docloom.packs.invoice.procedural import generate_catalogue
    from docloom.packs.invoice.validation import validate

    build_provenance: dict[str, object]
    if providers:
        typer.echo(f"building {companies:,} companies x {products_per_company} SKUs "
                   f"with an LLM (seed {seed})")
        rows, products, llm_report = _llm_build(
            providers, companies=companies, products_per_company=products_per_company,
            seed=seed, budget_usd=budget_usd, concurrency=concurrency, use_batch=batch,
        )
        typer.echo(f"  LLM filled {llm_report.llm_filled:,} of {llm_report.products:,} "
                   f"({llm_report.llm_fraction:.1%}), {llm_report.procedural_fallback:,} "
                   f"fell back, {llm_report.rounds} round(s), cost ${llm_report.total_cost}")
        build_provenance = {"seed": seed, **llm_report.summary()}
    else:
        typer.echo(f"building {companies:,} companies x {products_per_company} SKUs "
                   f"procedurally (seed {seed})")
        rows, products = generate_catalogue(
            companies=companies, products_per_company=products_per_company, seed=seed
        )
        build_provenance = {"generator": "procedural", "seed": seed,
                            "companies": len(rows),
                            "products_per_company": products_per_company}
    total = sum(len(v) for v in products.values())

    # Validate before writing, never after: a published artifact is copied,
    # cached and generated from for months, so a defect in it is far more
    # expensive than a defect in one run. Check the text that will actually
    # *print* — a French company's line items come from `fr`, so validating only
    # the English `description` would let a bad French string through.
    locale_of = {r.company_id: str(r.locale) for r in rows}

    def _printed(cid: str, product) -> str:
        return product.fr if locale_of[cid].startswith("fr") and product.fr else product.description

    report = validate(
        (f"{cid}:{i}", _printed(cid, product))
        for cid, items in products.items()
        for i, product in enumerate(items)
    )
    typer.echo(f"validated {report.checked:,} descriptions: "
               f"{report.rejected:,} rejected ({report.rejection_rate:.2%}), "
               f"{report.duplicate_groups:,} duplicate group(s)")
    for rule, count in sorted(report.by_rule().items(), key=lambda kv: -kv[1]):
        typer.echo(f"    {rule:16s} {count:,}")
    for finding in report.findings[:5]:
        typer.echo(f"    e.g. {finding.rule}: {finding.text[:60]!r}")

    if report.rejection_rate > max_rejection_rate:
        typer.echo(
            f"rejection rate {report.rejection_rate:.2%} exceeds the "
            f"{max_rejection_rate:.2%} ceiling — not publishing"
        )
        raise typer.Exit(1)

    manifest = write_catalogue(
        out, companies=rows, products=products, catalogue_version=version,
        # The artifact ships with its own audit: how it was built and what the
        # gates found, so a consumer can see it was checked rather than trust it.
        provenance={**build_provenance, "validation": report.summary()},
    )
    typer.echo(f"wrote {out} ({version}): {len(rows):,} companies, {total:,} products")
    for name, info in sorted(manifest.files.items()):
        typer.echo(f"    {name:20s} {info['rows']:,} rows  sha256 {info['sha256'][:12]}…")
    typer.echo(f"generate with:  docloom generate --catalogue {out} …")


@app.command()
def export(
    run_id: str = typer.Option(..., help="Run to export"),
    sink: str = typer.Option("./out/golden", envvar="DOCLOOM_SINK",
                             help="Golden sink URI (parquet:// | duckdb:// | bigquery://…)"),
    storage_prefix: str = typer.Option(
        "", "--storage-prefix",
        help="Sub-path the run's blobs live under (default: the run id); match what "
             "`generate` used for a nested run."),
    storage: str = _STORAGE,
) -> None:
    """Export a run's golden shards into a queryable sink."""
    blob = open_store(storage)
    target = open_sink(sink)
    stats = export_run(run_id, blob, target, storage_prefix=storage_prefix)
    if not stats.tables:
        typer.echo(f"no golden shards found for run {run_id!r} under {storage}")
        raise typer.Exit(1)
    for table, rows in sorted(stats.tables.items()):
        typer.echo(f"  {table}: {rows} row(s)")
    typer.echo(f"exported {stats.total_rows} row(s) across {len(stats.tables)} table(s) to {sink}")


@app.command()
def status(run_id: str = typer.Option(...), state: str = _STATE,
           wait: bool = typer.Option(False, "--wait",
                                     help="Poll until the run reaches a terminal state"),
           interval: float = typer.Option(5.0, "--interval",
                                          help="Seconds between polls with --wait")) -> None:
    """Show a run's progress. With --wait, stream it until the run finishes.

    This is the cheap way to follow a **detached** run: a dispatched Cloud Run job
    (``deploy.sh dispatch``) reports into the same state store, so
    ``status --wait --state firestore://…`` reattaches from any machine without
    holding a job open. A line is printed only when the counts change."""
    store = open_state(state)
    if not wait:
        _print_status(store, run_id)
        return

    import time
    last: object = None
    while True:
        run = store.get_run(run_id)
        if run is None:
            typer.echo(f"run {run_id!r} not found")
            raise typer.Exit(1)
        p = store.progress(run_id)
        key = (run.state, p[WorkUnitState.DONE], p[WorkUnitState.FAILED])
        if key != last:
            _print_status(store, run_id)
            last = key
        outstanding = p[WorkUnitState.PENDING] + p[WorkUnitState.RUNNING]
        terminal = run.state in (RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED)
        # Done when the run is terminal, or drained (nothing left to claim) — the
        # latter catches a run left RUNNING with failed units, which needs a resume.
        if terminal or outstanding == 0:
            clean = run.state is RunState.COMPLETED or (
                outstanding == 0 and p[WorkUnitState.FAILED] == 0)
            raise typer.Exit(0 if clean else 1)
        time.sleep(interval)


@app.command()
def pause(run_id: str = typer.Option(...), state: str = _STATE) -> None:
    """Pause a run: workers stop claiming new units."""
    open_state(state).set_run_state(run_id, RunState.PAUSED)
    typer.echo(f"paused {run_id}")


@app.command()
def cancel(run_id: str = typer.Option(...), state: str = _STATE) -> None:
    """Cancel a run: no further units will be claimed."""
    open_state(state).set_run_state(run_id, RunState.CANCELLED)
    typer.echo(f"cancelled {run_id}")


def _print_status(store, run_id: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        typer.echo(f"run {run_id!r} not found")
        raise typer.Exit(1)
    p = store.progress(run_id)
    typer.echo(
        f"run {run_id}: {run.state.value} — "
        f"{p[WorkUnitState.DONE]} done, {p[WorkUnitState.PENDING]} pending, "
        f"{p[WorkUnitState.RUNNING]} running, {p[WorkUnitState.FAILED]} failed "
        f"of {run.total_units} unit(s)"
    )


def _pdfs_progress(project, step, args):
    """A poll for the spinner's ``x/total`` on a local pdfs run — reads the run's
    unit progress from the workspace state store. None for other steps/targets."""
    from docloom.studio import Step
    if step is not Step.PDFS:
        return None
    state_uri = project.resources.get("state") or str(Path(project.root, "runs.db"))
    run_id = args.run_id

    def poll() -> str:
        try:
            store = open_state(state_uri)
            run = store.get_run(run_id)
            if run is None or not run.total_units:
                return ""
            done = store.progress(run_id).get(WorkUnitState.DONE, 0)
            return f"{done}/{run.total_units} units"
        except Exception:
            return ""
    return poll


def _run_studio(*, provider: str = "", project: str = "", step: str = "", pack: str = "",
                config: str = "", run_id: str = "", total: int = 0, catalogue: str = "",
                fmt: str = "pdf", condition: str = "", issue_date_from: str = "",
                issue_date_to: str = "", version: str = "v1", companies: int = 1000,
                products_per_company: int = 300, seed: int = 0, sink: str = "",
                mix: str = "procedural", budget_usd: float = 0.0, concurrency: int = 0,
                tasks: int = 0, parallelism: int = 0, rebuild_catalogue: bool = False,
                scaffold: str = "", onboard: bool = False, region: str = "", bucket: str = "",
                yes: bool = False, wait: bool = False, dry_run: bool = False) -> None:
    """The studio flow with real defaults — the `studio` command and a bare
    interactive `docloom` both call this. Walks target → project → pack → step →
    args, prompting only for what a flag left unset, with back/exit navigation."""
    from functools import partial

    from docloom.studio import Registry, Step, StudioError, available_targets, get_target, wizard
    from docloom.studio.app import run_step
    from docloom.studio.progress import drain_stdin, run_with_spinner
    from docloom.studio.prompts import BACK, EXIT, get_prompter, is_interactive

    interactive = is_interactive()
    prompter = get_prompter() if interactive else None
    loop = interactive and not dry_run       # loop back to the menu; back/exit offered

    registry = Registry()
    provider_flag = provider              # an explicit --provider pins the target

    def _args(step_enum: Step, one_shot: bool) -> object:
        # The first pass honours the per-run flags; later passes prompt afresh, so
        # a looped run can't silently reuse the same run id.
        rid, tot = (run_id, total) if one_shot else ("", 0)
        cat, cond = (catalogue, condition) if one_shot else ("", "")
        df, dt, cfg, snk = ((issue_date_from, issue_date_to, config, sink) if one_shot
                            else ("", "", "", ""))
        if step_enum is Step.CATALOG:
            mx, bud = (mix, budget_usd) if one_shot else ("procedural", 0.0)
            return wizard.build_catalogue_args(prompter, interactive, pack=pack_name,
                                               version=version, companies=companies,
                                               products_per_company=products_per_company,
                                               seed=seed, mix=mx, budget_usd=bud,
                                               concurrency=concurrency, tasks=tasks)
        if step_enum is Step.PDFS:
            return wizard.build_generate_args(prompter, interactive, pack=pack_name, run_id=rid,
                                              total=tot, catalogue=cat, fmt=fmt, condition=cond,
                                              date_from=df, date_to=dt, selection_file=cfg,
                                              tasks=tasks, parallelism=parallelism)
        return wizard.build_export_args(prompter, interactive, run_id=rid, sink=snk)

    provider_name: str | None = None          # None ⇒ (re)show the target screen
    target = None
    project_flag = project
    while True:                               # ── target + project screens ──
        if provider_name is None:             # ── target screen (the studio's entry) ──
            try:
                picked = wizard.choose_target(prompter, provider_flag, interactive, allow_exit=loop)
                target = None if picked == EXIT else get_target(picked)
            except StudioError as exc:
                raise typer.BadParameter(str(exc)) from exc
            if picked == EXIT:
                typer.echo("  bye.")
                return
            provider_name = picked
            project_flag = project            # a fresh target re-honours --project
        # Back is offered from the project screen only when the target was chosen
        # from a menu here (no --provider) with somewhere to return to.
        target_from_menu = (not provider_flag) and loop and len(available_targets()) > 1
        try:                                  # ── project screen ──
            proj = wizard.choose_project(prompter, provider_name, target, registry, project_flag,
                                         interactive=interactive, dry_run=dry_run, allow_back=loop,
                                         adopt=onboard, region=region, bucket=bucket)
            pack_name = wizard.choose_pack(prompter, pack, interactive)
        except StudioError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if proj == BACK:
            if target_from_menu:
                provider_name = None          # ← back to the target screen
                continue
            typer.echo("  bye.")              # nothing precedes → leave
            return
        project_flag = ""                     # a re-selection prompts rather than reusing the flag
        typer.echo(f"\n  project  {proj.ref}" + (f"   ({proj.root})" if proj.root else ""))

        step_flag, first, back = step, True, False
        while True:                           # ── step menu (returns here after each run) ──
            try:
                step_enum = wizard.choose_step(prompter, step_flag, interactive,
                                               allow_exit=loop, allow_back=loop)
            except StudioError as exc:
                raise typer.BadParameter(str(exc)) from exc
            step_flag = ""
            if step_enum == EXIT:
                typer.echo("  bye.")
                return
            if step_enum == BACK:
                back = True
                break
            try:
                args = _args(step_enum, first)
            except StudioError as exc:
                if loop:
                    typer.echo(f"  {exc}\n")
                    continue
                raise typer.BadParameter(str(exc)) from exc
            if args == BACK:                  # backed out of the args → step menu
                typer.echo("")
                continue
            first = False

            # Catalogue re-use: a rebuild costs money, so if one already exists for
            # this version, reuse it by default — scripted, --rebuild-catalogue
            # forces a rebuild; interactive, ask. (Skipped in --dry-run, which just
            # shows the build plan and shouldn't probe the target.)
            if step_enum is Step.CATALOG and not dry_run and not scaffold:
                existing = target.catalogue_info(proj, pack_name, args.version)
                if existing is not None:
                    rebuild = rebuild_catalogue
                    if interactive and prompter is not None and not rebuild_catalogue:
                        rebuild = prompter.confirm(
                            f"  catalogue '{args.version}' already exists "
                            f"({_catalogue_summary(existing)}) — rebuild (costs money)?",
                            default=False)
                    if not rebuild:
                        loc = target.catalogue_location(proj, pack_name, args.version)
                        typer.echo(f"\n  ✔ using existing catalogue '{args.version}' "
                                   f"({_catalogue_summary(existing)})\n    {loc}")
                        if not loop:
                            return
                        typer.echo("")
                        continue

            # --scaffold: write this step's config to a reusable deploy.sh run.yaml
            # and stop (gcp only — local has no run.yaml).
            if scaffold:
                if provider_name == "gcp":
                    target.scaffold(step_enum.value, proj, args, scaffold)
                    typer.echo(f"\n  ✔ scaffolded {step_enum.value} → {scaffold}"
                               f"\n    drive it:  deploy/gcp/deploy.sh -c {scaffold} <command>")
                else:
                    typer.echo("  --scaffold writes a deploy.sh run.yaml — gcp target only")
                if not loop:
                    return
                typer.echo("")
                continue

            # An LLM catalogue on a cloud target needs API keys in Secret Manager.
            # Record the secret *names* on the project (never a value), and tell the
            # operator how to create any that are missing — deploy.sh then verifies
            # them and grants access at build time.
            if step_enum is Step.CATALOG and provider_name != "local":
                proj = _capture_catalogue_secrets(registry, proj, args)

            preview = run_step(target, proj, step_enum, args, dry_run=True)
            typer.echo(f"\n  step     {step_enum.value}")
            typer.echo(f"  summary  {preview.summary}")
            typer.echo("  plan\n    " + (preview.command or "docloom " + " ".join(preview.argv)))
            if dry_run:
                typer.echo("\n  (dry run — nothing executed)")
                return
            if interactive and not yes and prompter is not None and not prompter.confirm(
                    "\n  Proceed?", default=True):
                typer.echo("  skipped.\n")
                continue

            prog = (_pdfs_progress(proj, step_enum, args)
                    if interactive and provider_name == "local" else None)
            do = partial(run_step, target, proj, step_enum, args, dry_run=False,
                         capture=interactive)
            result = run_with_spinner("working...", do, progress=prog) if interactive else do()
            drain_stdin()                     # drop keys typed during the (captured) run
            typer.echo(f"\n  {'✔' if result.ok else '✗'} {result.summary}")
            for line in result.detail.splitlines():
                typer.echo(f"    {line}")
            if result.links:
                typer.echo("  links")
                for link in result.links:
                    typer.echo(f"    {link.label:<10} {link.href}")
            if result.ok and result.run_id:
                registry.set_last_run(proj.ref, result.run_id)
            # A detached cloud run returns before it finishes; tell the operator how
            # to reattach, and stream it now if they asked to --wait.
            if (result.ok and result.run_id and step_enum is Step.PDFS
                    and provider_name != "local"):
                reattach = (f"docloom studio status -p {provider_name} "
                            f"--project {proj.id} --run {result.run_id}")
                typer.echo("  dispatched — not waiting. Reattach with:")
                typer.echo(f"    {reattach}")
                if wait:
                    typer.echo("  streaming until the run finishes (Ctrl-C to detach)…\n")
                    from docloom.studio.app import status_of
                    status_of(proj, result.run_id, wait=True)
            if not loop:
                if not result.ok:
                    raise typer.Exit(1)
                return
            typer.echo("")                    # spacing before the step menu returns
        if not back:                          # step loop only exits via return; guard anyway
            return


studio_app = typer.Typer(no_args_is_help=False,
                         help="Orchestrate catalog / pdfs / export — interactively or by flags.")
app.add_typer(studio_app, name="studio")


@studio_app.callback(invoke_without_command=True)
def studio(
    ctx: typer.Context,
    provider: str = typer.Option("", "--provider", "-p",
                                 help="Deployment target — local | gcp; omit to pick on the "
                                      "first screen (aws/azure land later)"),
    project: str = typer.Option("", "--project",
                                help="Project / local workspace; created & saved if new"),
    onboard: bool = typer.Option(False, "--onboard",
                                 help="Onboard an existing cloud project (register, no provision)"),
    region: str = typer.Option("", "--region", help="Cloud region (with a new/onboarded project)"),
    bucket: str = typer.Option("", "--bucket", help="Cloud bucket (with a new/onboarded project)"),
    step: str = typer.Option("", "--step", help="catalog | pdfs | export"),
    pack: str = typer.Option("", "--pack", help="Document type (pack); '' = sole installed"),
    config: str = typer.Option("", "--config", help="Selection/slice file for the pdfs step"),
    run_id: str = typer.Option("", "--run-id", help="Run id (pdfs, export)"),
    total: int = typer.Option(0, "--total", help="How many documents (pdfs)"),
    catalogue: str = typer.Option("", "--catalogue", help="Catalogue uri (pdfs); '' = seed"),
    fmt: str = typer.Option("pdf", "--format", help="pdf | html (pdfs)"),
    condition: str = typer.Option("", "--condition", help="Capture condition (pdfs)"),
    issue_date_from: str = typer.Option("", "--issue-date-from", help="Issue-date start (pdfs)"),
    issue_date_to: str = typer.Option("", "--issue-date-to", help="Issue-date end (pdfs)"),
    version: str = typer.Option("v1", "--version", help="Catalogue version (catalog)"),
    companies: int = typer.Option(1000, "--companies", help="Issuers (catalog)"),
    products_per_company: int = typer.Option(300, "--products-per-company",
                                              help="SKUs each (catalog)"),
    seed: int = typer.Option(0, "--seed", help="Build seed (catalog)"),
    mix: str = typer.Option("procedural", "--mix",
                            help="LLM provider mix (catalog): procedural | cheap-mix | "
                                 "balanced | anthropic"),
    budget_usd: float = typer.Option(0.0, "--budget", help="Hard USD cap for an LLM catalogue"),
    concurrency: int = typer.Option(0, "--concurrency",
                                    help="LLM calls in flight for the catalog build (default 8)"),
    tasks: int = typer.Option(0, "--tasks",
                              help="Cloud Run tasks: catalog>1 shards the build; pdfs workers "
                                   "(default catalog 1, pdfs 4)"),
    parallelism: int = typer.Option(0, "--parallelism",
                                    help="Max pdfs tasks running at once (default = --tasks)"),
    rebuild_catalogue: bool = typer.Option(
        False, "--rebuild-catalogue",
        help="Rebuild the catalog even if one exists for this version (costs money)"),
    scaffold: str = typer.Option(
        "", "--scaffold",
        help="Write the step's deploy.sh run.yaml to this path and stop (gcp only)"),
    sink: str = typer.Option("", "--sink", help="Golden sink uri (export); '' = local DuckDB"),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation"),
    wait: bool = typer.Option(False, "--wait",
                              help="After a cloud dispatch, stream status until the run finishes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; run nothing"),
) -> None:
    """Orchestrate catalog / pdfs / export — interactively, or fully by flags.

    In a terminal it walks target → project → pack → step → args, prompting only
    for what a flag did not already supply, and confirms before running. Fully
    flagged (or piped) it runs non-interactively. A cloud run is **dispatched** and
    returns handles + links immediately — follow it with `docloom studio status`.
    See `feature_explorations/interactive-cli-studio.md`.
    """
    if ctx.invoked_subcommand is not None:
        return          # a subcommand (e.g. `status`) handles it; don't run the wizard
    _run_studio(provider=provider, project=project, step=step, pack=pack, config=config,
                run_id=run_id, total=total, catalogue=catalogue, fmt=fmt, condition=condition,
                issue_date_from=issue_date_from, issue_date_to=issue_date_to, version=version,
                companies=companies, products_per_company=products_per_company, seed=seed,
                mix=mix, budget_usd=budget_usd, concurrency=concurrency, tasks=tasks,
                parallelism=parallelism, rebuild_catalogue=rebuild_catalogue, scaffold=scaffold,
                sink=sink, onboard=onboard, region=region,
                bucket=bucket, yes=yes, wait=wait, dry_run=dry_run)


@studio_app.command("status")
def studio_status(
    run: str = typer.Option(..., "--run", "--run-id", help="The run id to reattach to"),
    project: str = typer.Option("", "--project",
                                help="Saved project ref (<target>:<id>) or id; '' = the default"),
    provider: str = typer.Option("", "--provider", "-p",
                                 help="Target for a bare --project id (local | gcp)"),
    wait: bool = typer.Option(False, "--wait", help="Stream until the run is terminal"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command; run nothing"),
) -> None:
    """Reattach to a dispatched run and show its progress (`--wait` streams).

    Resolves the run's state store from the saved project, so it works from any
    machine that has the project registered — no live job or terminal to hold."""
    from docloom.studio import Registry, StudioError
    from docloom.studio.app import status_of
    try:
        proj = _resolve_saved_project(Registry(), project, provider)
        result = status_of(proj, run, wait=wait, dry_run=dry_run)
    except StudioError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        typer.echo(f"  project  {proj.ref}\n  plan\n    {result.command}")
        return
    if not result.ok:
        raise typer.Exit(1)


@studio_app.command("projects")
def studio_projects() -> None:
    """List the saved projects (from ~/.docloom/projects.yaml)."""
    from docloom.studio import Registry
    reg = Registry()
    projects = reg.projects()
    if not projects:
        typer.echo("  no saved projects — run `docloom studio` to create or onboard one")
        return
    default = reg.default_ref()
    for p in projects:
        where = p.root or (f"{p.region} · gs://{p.bucket}" if p.bucket else "")
        when = (f"provisioned {p.provisioned_at}" if p.provisioned_at
                else f"created {p.created_at}" if p.created_at else "")
        last = f" · last run {p.last_run}" if p.last_run else ""
        star = " *" if p.ref == default else ""
        typer.echo(f"  {p.target:<6} {p.id:<24} {where}  {when}{last}{star}")


@studio_app.command("provision")
def studio_provision(
    project: str = typer.Option("", "--project", help="Saved project ref or id; '' = the default"),
    provider: str = typer.Option("", "--provider", "-p", help="Target for a bare --project id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve the project; run nothing"),
) -> None:
    """(Re)provision the selected project — idempotent, safe to re-run."""
    from docloom.studio import Registry, StudioError, get_target
    from docloom.studio.types import ProjectSpec
    reg = Registry()
    try:
        proj = _resolve_saved_project(reg, project, provider)
        target = get_target(proj.target)
    except StudioError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"  provisioning {proj.ref}…")
    if dry_run:
        typer.echo("  (dry run — nothing created)")
        return
    spec = ProjectSpec(target=proj.target, id=proj.id, region=proj.region,
                       bucket=proj.bucket, root=proj.root)
    updated = target.provision(spec)
    reg.add(updated)
    typer.echo(f"  ✔ provisioned {proj.ref}")


@studio_app.command("teardown")
def studio_teardown(
    project: str = typer.Option(..., "--project", help="Saved project ref or id (required)"),
    provider: str = typer.Option("", "--provider", "-p", help="Target for a bare --project id"),
    delete_data: bool = typer.Option(
        False, "--delete-data",
        help="Also delete the bucket / local workspace (documents + golden) — irreversible"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; delete nothing"),
) -> None:
    """Delete a project's resources and forget it. Keeps its data unless
    --delete-data. Cloud teardown leaves Firestore + the service account in place."""
    from docloom.studio import Registry, StudioError, get_target
    from docloom.studio.prompts import get_prompter, is_interactive
    reg = Registry()
    try:
        proj = _resolve_saved_project(reg, project, provider)
        target = get_target(proj.target)
    except StudioError as exc:
        raise typer.BadParameter(str(exc)) from exc

    scope = "DELETES ALL DATA (bucket/workspace)" if delete_data else "keeps the data"
    if not dry_run and not yes:
        # Destructive — require an explicit confirmation, interactively or via --yes.
        if not is_interactive():
            raise typer.BadParameter(f"teardown of {proj.ref} needs confirmation — pass --yes")
        if not get_prompter().confirm(f"  Tear down {proj.ref} — {scope}?", default=False):
            typer.echo("  aborted")
            return
    result = target.teardown(proj, keep_data=not delete_data, dry_run=dry_run)
    if dry_run:
        typer.echo(f"  project  {proj.ref}\n  summary  {result.summary}")
        if result.command:
            typer.echo(f"  plan\n    {result.command}")
        return
    if not result.ok:
        for line in result.detail.splitlines():
            typer.echo(f"    {line}")
        raise typer.Exit(1)
    reg.remove(proj.ref)
    typer.echo(f"  ✔ {result.summary} · removed from the registry")


def _catalogue_summary(info: dict) -> str:
    """A one-line description of an existing catalogue for the reuse prompt."""
    bits = []
    if info.get("created_at"):
        bits.append(f"built {str(info['created_at'])[:10]}")
    prov = info.get("provenance") or {}
    for key in ("total_cost", "cost", "spend"):
        if prov.get(key) is not None:
            bits.append(f"${prov[key]}")
            break
    return " · ".join(bits) or "exists"


def _capture_catalogue_secrets(registry, project, args):
    """For an LLM catalogue on a cloud target: record the mix's secret *names* on
    the project (never a value) and print how to create any missing ones. Returns
    the (possibly updated) project. A procedural build needs no keys — a no-op."""
    from dataclasses import replace

    from docloom.studio.mixes import get_mix
    mix = get_mix(getattr(args, "mix", "procedural"))
    if not mix.is_llm:
        return project
    secrets = mix.secrets_map()                       # env var → Secret Manager name
    names = tuple(secrets.values())
    typer.echo(f"\n  this mix needs {len(names)} Secret Manager secret(s) "
               "(names saved to the project; values stay in Secret Manager):")
    for env, name in secrets.items():
        typer.echo(f"    {env:<20} → {name}")
    typer.echo("  create any that are missing (once), then the build reads them:")
    for name in names:
        typer.echo(f'    printf %s "$YOUR_KEY" | gcloud secrets create {name} '
                   f"--data-file=- --project={project.id}")
    merged = tuple(sorted(set(project.secrets_present) | set(names)))
    if merged != project.secrets_present:
        project = replace(project, secrets_present=merged)
        registry.add(project)                          # persist names only
    return project


def _resolve_saved_project(registry, project: str, provider: str):
    """The saved :class:`Project` named by ``--project`` (a ``<target>:<id>`` ref,
    a bare id with ``--provider``, a unique bare id, or the registry default)."""
    from docloom.studio import StudioError
    if ":" in project:
        ref = project
    elif provider and project:
        ref = f"{provider}:{project}"
    elif project:
        matches = [p for p in registry.projects() if p.id == project]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise StudioError(f"no saved project with id {project!r}; "
                              "run `docloom studio` to create or onboard it")
        raise StudioError(f"project id {project!r} is ambiguous across targets — pass --provider")
    else:
        ref = registry.default_ref()
        if not ref:
            raise StudioError("no --project given and no default is set — pass --project <ref>")
    proj = registry.get(ref)
    if proj is None:
        raise StudioError(f"no saved project {ref!r}; run `docloom studio` to create or onboard it")
    return proj


if __name__ == "__main__":
    app()
