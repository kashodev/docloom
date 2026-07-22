"""CLI tests via typer's CliRunner.

The full user-facing workflow with no cloud and no keys: generate (HTML for
speed, no Chromium), status, export to DuckDB, and the lifecycle commands. Each
run points storage/state/sink at a tmp dir.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from docloom.cli import app

runner = CliRunner()


def _paths(tmp_path: Path) -> dict[str, str]:
    return {
        "storage": str(tmp_path / "blobs"),
        "state": str(tmp_path / "runs.db"),
        "sink": str(tmp_path / "golden.db"),
    }


def test_generate_then_status_then_export(tmp_path: Path) -> None:
    p = _paths(tmp_path)

    gen = runner.invoke(app, [
        "generate", "--run-id", "cli1", "--total", "12", "--unit-size", "4",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "12 document(s)" in gen.output
    assert "completed" in gen.output

    st = runner.invoke(app, ["status", "--run-id", "cli1", "--state", p["state"]])
    assert st.exit_code == 0
    assert "3 done" in st.output          # 12 / 4 = 3 units

    exp = runner.invoke(app, [
        "export", "--run-id", "cli1",
        "--storage", p["storage"], "--sink", f"duckdb:///{p['sink']}",
    ])
    assert exp.exit_code == 0, exp.output
    assert "invoices: 12 row(s)" in exp.output
    assert "line_items" in exp.output


def test_generate_is_resumable(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    runner.invoke(app, [
        "generate", "--run-id", "cli2", "--total", "8", "--unit-size", "4",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ])
    # A second generate with --resume is a clean no-op (nothing failed).
    again = runner.invoke(app, [
        "generate", "--run-id", "cli2", "--total", "8", "--unit-size", "4",
        "--format", "html", "--resume", "--storage", p["storage"], "--state", p["state"],
    ])
    assert again.exit_code == 0
    assert "re-queued 0 failed unit(s)" in again.output


def test_export_of_missing_run_exits_nonzero(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    res = runner.invoke(app, [
        "export", "--run-id", "ghost", "--storage", p["storage"],
        "--sink", f"duckdb:///{p['sink']}",
    ])
    assert res.exit_code == 1
    assert "no golden shards" in res.output


def test_unknown_pack_is_rejected(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    res = runner.invoke(app, [
        "generate", "--run-id", "x", "--total", "1", "--pack", "contract",
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert res.exit_code != 0
    assert "unknown pack" in res.output


def test_pause_and_cancel(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    runner.invoke(app, [
        "generate", "--run-id", "cli3", "--total", "4", "--unit-size", "4",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ])
    paused = runner.invoke(app, ["pause", "--run-id", "cli3", "--state", p["state"]])
    assert paused.exit_code == 0 and "paused" in paused.output

    cancelled = runner.invoke(app, ["cancel", "--run-id", "cli3", "--state", p["state"]])
    assert cancelled.exit_code == 0 and "cancelled" in cancelled.output


def test_plan_then_generate_works_the_planned_run(tmp_path: Path) -> None:
    """`plan` exists so a large run can be inspected before compute is committed
    to it; `generate` must then pick that plan up rather than making its own."""
    p = _paths(tmp_path)

    pl = runner.invoke(app, [
        "plan", "--run-id", "cli_plan", "--total", "12", "--unit-size", "4",
        "--state", p["state"],
    ])
    assert pl.exit_code == 0, pl.output
    assert "planned run cli_plan: 12 document(s) in 3 unit(s)" in pl.output
    assert "3 pending" in pl.output

    gen = runner.invoke(app, [
        "generate", "--run-id", "cli_plan", "--total", "12", "--unit-size", "4",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "12 document(s)" in gen.output

    st = runner.invoke(app, ["status", "--run-id", "cli_plan", "--state", p["state"]])
    assert "3 done" in st.output          # the original plan, not a second one


def test_planning_twice_is_harmless(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    args = ["plan", "--run-id", "twice", "--total", "8", "--unit-size", "4",
            "--state", p["state"]]
    assert runner.invoke(app, args).exit_code == 0
    again = runner.invoke(app, args)
    assert again.exit_code == 0, again.output
    assert "already planned: 2 unit(s)" in again.output


def test_plan_rejects_an_unknown_pack(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    bad = runner.invoke(app, [
        "plan", "--run-id", "nope", "--total", "4", "--pack", "nonsense",
        "--state", p["state"],
    ])
    assert bad.exit_code != 0
    assert "unknown pack" in bad.output


def test_composition_flags_constrain_the_run(tmp_path: Path) -> None:
    """`--locale` has to reach the sampler, not just be accepted."""
    p = _paths(tmp_path)
    gen = runner.invoke(app, [
        "generate", "--run-id", "fr", "--total", "4", "--unit-size", "4",
        "--format", "html", "--locale", "fr-FR", "--max-line-items", "3",
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "composition: locales=fr-FR" in gen.output

    exp = runner.invoke(app, [
        "export", "--run-id", "fr",
        "--storage", p["storage"], "--sink", f"duckdb:///{p['sink']}",
    ])
    assert exp.exit_code == 0, exp.output

    import duckdb
    rows = duckdb.connect(p["sink"]).execute(
        "select distinct locale, currency from invoices"
    ).fetchall()
    assert rows == [("fr-FR", "EUR")], rows


def test_a_selection_file_drives_the_same_composition(tmp_path: Path) -> None:
    """One parser behind both surfaces, so a file and flags cannot drift."""
    p = _paths(tmp_path)
    sel = tmp_path / "slice.yaml"
    sel.write_text("locales: [en-GB]\ncondition: handwritten\nwear: crisp\n")
    gen = runner.invoke(app, [
        "generate", "--run-id", "uk", "--total", "2", "--unit-size", "2",
        "--format", "html", "--max-line-items", "3", "--selection-file", str(sel),
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "locales=en-GB" in gen.output and "wear=0-0.25" in gen.output


def test_a_flag_overrides_the_selection_file(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    sel = tmp_path / "slice.yaml"
    sel.write_text("locales: [en-GB]\n")
    gen = runner.invoke(app, [
        "generate", "--run-id", "ov", "--total", "2", "--unit-size", "2",
        "--format", "html", "--max-line-items", "3", "--selection-file", str(sel),
        "--locale", "fr-FR", "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "locales=fr-FR" in gen.output


def test_an_impossible_composition_fails_the_run_loudly(tmp_path: Path) -> None:
    """Not a silent fallback to an unconstrained draw — that is the whole point."""
    p = _paths(tmp_path)
    gen = runner.invoke(app, [
        "generate", "--run-id", "bad", "--total", "2", "--unit-size", "2",
        "--format", "html", "--locale", "de-DE",
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code != 0
    assert "de-DE" in gen.output or "de-DE" in str(gen.exception)


def test_a_malformed_wear_is_rejected_before_anything_runs(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    bad = runner.invoke(app, [
        "generate", "--run-id", "w", "--total", "2", "--wear", "pristine",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ])
    assert bad.exit_code != 0
    assert "unknown wear" in bad.output
