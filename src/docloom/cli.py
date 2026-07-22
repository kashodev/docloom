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
) -> None:
    """Generate documents and golden shards for a run."""
    if pack not in available_packs():
        raise typer.BadParameter(f"unknown pack {pack!r}; available: {', '.join(available_packs())}")

    doc_pack = get_pack(pack)
    blob = open_store(storage)
    store = open_state(state)
    usage = open_usage_sink(llm_usage, blob=blob, run_id=run_id)
    source = doc_pack.default_source()
    renderer = PdfRenderer(doc_pack) if fmt == "pdf" else HtmlRenderer(doc_pack)

    if resume:
        requeued = resume_run(store, run_id)
        typer.echo(f"resume: re-queued {requeued} failed unit(s)")
    elif store.get_run(run_id) is None:
        create_run(store, run_id=run_id, pack=pack, config_id=config_id,
                   total=total, unit_size=unit_size)
        typer.echo(f"planned run {run_id}: {total} documents in units of {unit_size}")

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
