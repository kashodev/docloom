"""Studio phase 1 — registry, the local target, and the flag-driven command.

Everything is exercised with `dry_run` (argv resolution) or a temp registry, so
the suite never renders a document or touches a cloud — the plumbing is proven
without the payload.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from docloom.cli import app
from docloom.studio import (
    CatalogueArgs,
    ExportArgs,
    GenerateArgs,
    LocalTarget,
    Project,
    ProjectSpec,
    Registry,
    Step,
    StudioError,
    get_target,
)
from docloom.studio.app import resolve_project, run_step

runner = CliRunner()


# ── registry ────────────────────────────────────────────────────────────────
def test_registry_round_trips_a_project(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="local", id="ws", root="/w"), make_default=True)
    (got,) = reg.projects()
    assert got.ref == "local:ws" and got.root == "/w"
    assert reg.default_ref() == "local:ws"
    assert reg.get("local:ws") is not None and reg.get("gcp:none") is None


def test_add_is_idempotent_not_duplicating(tmp_path: Path) -> None:
    """Re-provisioning the same project adopts it — one row, not two."""
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="local", id="ws", root="/one"))
    reg.add(Project(target="local", id="ws", root="/two"))
    assert [p.root for p in reg.projects()] == ["/two"]      # replaced, not appended


def test_last_run_is_recorded(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="local", id="ws", root="/w"))
    reg.set_last_run("local:ws", "run-42")
    assert reg.get("local:ws").last_run == "run-42"


def test_registry_stores_secret_names_never_values(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="gcp", id="p", secrets_present=("DEEPSEEK_API_KEY",)))
    text = (tmp_path / "projects.yaml").read_text()
    assert "DEEPSEEK_API_KEY" in text                        # the name is fine
    assert reg.get("gcp:p").secrets_present == ("DEEPSEEK_API_KEY",)


# ── local target ────────────────────────────────────────────────────────────
def test_provision_makes_a_workspace(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.provision(ProjectSpec(target="local", id="ws", root=str(tmp_path / "ws")))
    assert (Path(p.root) / "blobs").is_dir()
    assert t.is_provisioned(p)


def test_normalise_has_no_side_effects(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path / "ws")))
    assert not (Path(p.root) / "blobs").exists()             # nothing written
    assert not t.is_provisioned(p)


def test_generate_argv_carries_the_composition(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path)))
    r = t.run_generate(p, GenerateArgs(run_id="r", total=5, catalogue="gs://b/v2",
                                       condition="clean", date_from="2023-01-01",
                                       date_to="2025-12-31"), dry_run=True)
    a = r.argv
    assert a[0] == "generate"
    for flag, val in [("--run-id", "r"), ("--total", "5"), ("--catalogue", "gs://b/v2"),
                      ("--condition", "clean"), ("--issue-date-from", "2023-01-01")]:
        assert val == a[a.index(flag) + 1]
    assert "--storage" in a and "--state" in a
    assert any(link.label == "documents" for link in r.links)


def test_catalogue_argv_is_procedural_and_keyless(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path)))
    r = t.run_catalogue(p, CatalogueArgs(version="v2", companies=50), dry_run=True)
    assert r.argv[0] == "catalogue"
    assert "--providers" not in r.argv                       # key-free build
    assert r.argv[r.argv.index("--version") + 1] == "v2"


def test_export_defaults_to_a_local_duckdb(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path)))
    r = t.run_export(p, ExportArgs(run_id="r"), dry_run=True)
    sink = r.argv[r.argv.index("--sink") + 1]
    assert sink.startswith("duckdb://") and sink.endswith("corpus.duckdb")


# ── target lookup ───────────────────────────────────────────────────────────
def test_get_target_local_and_the_not_yet_message() -> None:
    assert get_target("local").name == "local"
    with pytest.raises(StudioError, match="phase 3"):
        get_target("gcp")
    with pytest.raises(StudioError, match="unknown deployment target"):
        get_target("nope")


# ── resolve_project ─────────────────────────────────────────────────────────
def test_resolve_creates_and_saves_a_new_project(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    p = resolve_project(reg, LocalTarget(), "local", str(tmp_path / "ws"))
    assert (Path(p.root) / "blobs").is_dir()                 # provisioned
    assert reg.get(p.ref) is not None                        # saved


def test_resolve_dry_run_neither_provisions_nor_saves(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    p = resolve_project(reg, LocalTarget(), "local", str(tmp_path / "ws"), dry_run=True)
    assert not (Path(p.root) / "blobs").exists()
    assert reg.projects() == []                              # nothing written


def test_resolve_adopts_an_existing_project(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="local", id="ws", root=str(tmp_path / "ws"), last_run="old"))
    p = resolve_project(reg, LocalTarget(), "local", str(tmp_path / "ws"))
    assert p.last_run == "old"                               # reused, not recreated


def test_resolve_without_a_project_and_no_default_is_an_error(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    with pytest.raises(StudioError, match="no project selected"):
        resolve_project(reg, LocalTarget(), "local", "")


def test_run_step_dispatches_by_step(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path)))
    assert run_step(t, p, Step.CATALOG, CatalogueArgs(), dry_run=True).argv[0] == "catalogue"
    assert run_step(t, p, Step.EXPORT, ExportArgs(run_id="r"), dry_run=True).argv[0] == "export"


# ── the command ─────────────────────────────────────────────────────────────
def test_studio_pdfs_dry_run_prints_the_plan(tmp_path: Path) -> None:
    env = {"DOCLOOM_HOME": str(tmp_path / ".docloom")}
    argv = ["studio", "-p", "local", "--project", str(tmp_path / "ws"),
            "--step", "pdfs", "--run-id", "r", "--total", "2", "--dry-run"]
    res = runner.invoke(app, argv, env=env)
    assert res.exit_code == 0, res.output
    assert "docloom generate" in res.output and "--run-id r" in res.output
    assert "dry run" in res.output


def test_studio_requires_a_step(tmp_path: Path) -> None:
    env = {"DOCLOOM_HOME": str(tmp_path / ".docloom")}
    res = runner.invoke(app, ["studio", "-p", "local", "--project", str(tmp_path / "ws")], env=env)
    assert res.exit_code != 0
    assert "step" in res.output


def test_studio_export_needs_a_run_id(tmp_path: Path) -> None:
    env = {"DOCLOOM_HOME": str(tmp_path / ".docloom")}
    res = runner.invoke(app, ["studio", "-p", "local", "--project", str(tmp_path / "ws"),
                              "--step", "export", "--dry-run"], env=env)
    assert res.exit_code != 0
    assert "run-id" in res.output
