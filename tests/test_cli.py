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
