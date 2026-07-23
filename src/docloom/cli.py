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
from docloom.core.selection import Selection
from docloom.core.sinks import open_sink
from docloom.core.state import open_state
from docloom.core.storage import open_store
from docloom.core.usage import DEFAULT_USAGE_URI, open_usage_sink

app = typer.Typer(add_completion=False, help="Generate synthetic documents with a golden dataset.")

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
    catalogue: str = _CATALOGUE,
) -> None:
    """Generate documents and golden shards for a run."""
    if pack not in available_packs():
        raise typer.BadParameter(f"unknown pack {pack!r}; available: {', '.join(available_packs())}")

    selection = _selection_from(
        selection_file, locale=locale, company=company, company_count=company_count,
        archetype=archetype, archetype_count=archetype_count, business_type=business_type,
        condition=condition, wear=wear, goods_receipt=goods_receipt,
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
