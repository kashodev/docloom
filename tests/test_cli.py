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


def test_a_worker_retrying_into_an_all_failed_run_still_exits_nonzero(
    tmp_path: Path, monkeypatch,
) -> None:
    """The false green a whole smoke run hid behind.

    First attempt: rendering fails, every unit goes FAILED, exit 1 → Cloud Run
    retries the task. Second attempt (not --resume): a fresh worker finds those
    units already FAILED and out of the claimable pool, so it claims nothing and
    records no failures of its own. Exiting on *its* stats would report success
    over a run that produced zero documents. It must read the run's state."""
    from docloom.core.pipeline import HtmlRenderer

    def boom(self, record):  # noqa: ANN001, ANN202
        raise RuntimeError("render exploded")

    monkeypatch.setattr(HtmlRenderer, "render", boom)
    p = _paths(tmp_path)
    args = [
        "generate", "--run-id", "fg", "--total", "6", "--unit-size", "3",
        "--format", "html", "--storage", p["storage"], "--state", p["state"],
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 1, first.output               # this worker failed units

    second = runner.invoke(app, args)                        # retry, no --resume
    assert second.exit_code == 1, (
        "a retry that claimed nothing reported success over an all-failed run"
    )
    assert "0 done" not in second.output or "2 failed" in second.output


def test_generate_draws_from_a_catalogue_artifact(tmp_path: Path) -> None:
    """The artifact reaches the sampler and is recorded on the golden rows —
    reading a Parquet file needs no credential, so this stays local-first."""
    from decimal import Decimal as D

    from docloom.core.locale.enums import Currency, Locale
    from docloom.packs.invoice.artifact import CompanyRow, write_catalogue
    from docloom.packs.invoice.catalog import ProductTemplate
    from docloom.packs.invoice.enums import BusinessType
    from docloom.packs.invoice.jurisdictions import Jurisdiction

    art = tmp_path / "catalogue"
    rows = [CompanyRow("solo", "Solo Supply Ltd", BusinessType.RETAIL,
                       Jurisdiction.US, Locale.EN_US, Currency.USD, 1.0)]
    write_catalogue(str(art), companies=rows,
                    products={"solo": [ProductTemplate(f"Bespoke part {i}", D("3.00"), D("9.00"))
                                       for i in range(12)]},
                    catalogue_version="cli-test-1")

    p = _paths(tmp_path)
    gen = runner.invoke(app, [
        "generate", "--run-id", "cat", "--total", "4", "--unit-size", "4",
        "--format", "html", "--max-line-items", "5", "--catalogue", str(art),
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "cli-test-1" in gen.output

    exp = runner.invoke(app, [
        "export", "--run-id", "cat",
        "--storage", p["storage"], "--sink", f"duckdb:///{p['sink']}",
    ])
    assert exp.exit_code == 0, exp.output

    import duckdb
    conn = duckdb.connect(p["sink"])
    assert conn.execute("select distinct catalogue_version from invoices").fetchall() == [
        ("cli-test-1",)
    ]
    descriptions = conn.execute("select distinct description from line_items").fetchall()
    assert all(d[0].startswith("Bespoke part") for d in descriptions), descriptions


def test_a_missing_catalogue_fails_before_generating(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    bad = runner.invoke(app, [
        "generate", "--run-id", "nocat", "--total", "2", "--format", "html",
        "--catalogue", str(tmp_path / "absent"),
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert bad.exit_code != 0


# ── docloom catalogue ───────────────────────────────────────────────────────
def test_catalogue_builds_validates_and_generates(tmp_path: Path) -> None:
    """The full step-5 loop: build a pool, gate it, publish it, generate from
    it — with no API key anywhere."""
    art = tmp_path / "cat"
    built = runner.invoke(app, [
        "catalogue", "--out", str(art), "--version", "v1",
        "--companies", "12", "--products-per-company", "30", "--seed", "1",
    ])
    assert built.exit_code == 0, built.output
    assert "validated 360 descriptions" in built.output
    assert "0 rejected" in built.output
    assert (art / "manifest.json").is_file()

    p = _paths(tmp_path)
    gen = runner.invoke(app, [
        "generate", "--run-id", "fromcat", "--total", "6", "--unit-size", "6",
        "--format", "html", "--max-line-items", "6", "--catalogue", str(art),
        "--storage", p["storage"], "--state", p["state"],
    ])
    assert gen.exit_code == 0, gen.output
    assert "v1" in gen.output


def test_the_manifest_carries_the_validation_audit(tmp_path: Path) -> None:
    """An artifact ships with its own audit, so a consumer can see it was
    checked rather than take it on trust."""
    import json

    art = tmp_path / "cat"
    runner.invoke(app, [
        "catalogue", "--out", str(art), "--version", "v1",
        "--companies", "6", "--products-per-company", "20",
    ])
    provenance = json.loads((art / "manifest.json").read_text())["provenance"]
    assert provenance["generator"] == "procedural"
    assert provenance["validation"]["checked"] == 120
    assert provenance["validation"]["rejected"] == 0
    assert "by_rule" in provenance["validation"]


def test_a_catalogue_for_an_unknown_pack_is_rejected(tmp_path: Path) -> None:
    bad = runner.invoke(app, [
        "catalogue", "--out", str(tmp_path / "x"), "--version", "v1",
        "--pack", "contract",
    ])
    assert bad.exit_code != 0
    assert "no catalogue builder" in bad.output
