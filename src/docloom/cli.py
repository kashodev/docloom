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
from docloom.core.pipeline import (
    HtmlRenderer,
    PdfRenderer,
    create_run,
    export_run,
    resume_run,
    work_run,
)
from docloom.core.logging import get_logger
from docloom.core.selection import Selection
from docloom.core.sinks import open_sink
from docloom.core.state import open_state
from docloom.core.storage import open_store
from docloom.core.usage import DEFAULT_USAGE_URI, open_usage_sink

_log = get_logger(__name__)
app = typer.Typer(add_completion=False, help="Generate synthetic documents with a golden dataset.")


@app.callback()
def _init() -> None:
    """Configure logging before any command runs. Console at a terminal, JSON to
    a pipe/Cloud Run; DOCLOOM_LOG_LEVEL / DOCLOOM_LOG_FORMAT override."""
    from docloom.core.logging import configure
    configure()

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
        stats = work_run(store, run_id=run_id, source=source, renderer=renderer, blob=blob)
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
               seed: int, budget_usd: float, concurrency: int, use_batch: bool):  # noqa: ANN202
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


def _dec(value: float):  # noqa: ANN202
    from decimal import Decimal
    return Decimal(str(value))


def _sharded_catalogue(*, out: str, version: str, companies: int, products_per_company: int,
                       seed: int, unit_size: int, build_id: str, state_uri: str,
                       providers: str, budget_usd: float, concurrency: int) -> None:
    """A sharded, resumable build coordinated through a StateStore. Safe to run
    from many tasks at once — the atomic claim splits the company units between
    them, and whoever finishes last writes the root manifest."""
    from docloom.core.providers.factory import build_mix
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


def _resolve_mix(providers: str):  # noqa: ANN202
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

    def _printed(cid: str, product) -> str:  # noqa: ANN001
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
    storage: str = _STORAGE,
) -> None:
    """Export a run's golden shards into a queryable sink."""
    blob = open_store(storage)
    target = open_sink(sink)
    stats = export_run(run_id, blob, target)
    if not stats.tables:
        typer.echo(f"no golden shards found for run {run_id!r} under {storage}")
        raise typer.Exit(1)
    for table, rows in sorted(stats.tables.items()):
        typer.echo(f"  {table}: {rows} row(s)")
    typer.echo(f"exported {stats.total_rows} row(s) across {len(stats.tables)} table(s) to {sink}")


@app.command()
def status(run_id: str = typer.Option(...), state: str = _STATE) -> None:
    """Show a run's progress."""
    _print_status(open_state(state), run_id)


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


def _print_status(store, run_id: str) -> None:  # noqa: ANN001
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


if __name__ == "__main__":
    app()
