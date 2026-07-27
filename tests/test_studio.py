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
    GcpTarget,
    GenerateArgs,
    LocalTarget,
    Project,
    ProjectSpec,
    Registry,
    Result,
    Step,
    StudioError,
    get_target,
    wizard,
)
from docloom.studio.app import resolve_project, run_step
from docloom.studio.prompts import (
    BACK,
    EXIT,
    Choice,
    FallbackPrompter,
    ScriptedPrompter,
    get_prompter,
)

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
def test_get_target_local_gcp_and_the_not_yet_message() -> None:
    assert get_target("local").name == "local"
    assert get_target("gcp").name == "gcp"
    with pytest.raises(StudioError, match="not available yet"):
        get_target("aws")
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


# ── prompts ─────────────────────────────────────────────────────────────────
def test_fallback_prompter_reads_input(monkeypatch) -> None:
    answers = iter(["2", "y", "hello"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    p = FallbackPrompter()
    assert p.select("pick", [Choice("a", "A"), Choice("b", "B")]) == "b"
    assert p.confirm("ok?") is True
    assert p.text("name?") == "hello"


def test_fallback_prompter_uses_defaults_on_empty(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    p = FallbackPrompter()
    assert p.select("pick", [Choice("a", "A")], default="a") == "a"
    assert p.text("name?", default="d") == "d"
    assert p.confirm("ok?", default=True) is True


def test_scripted_prompter_pops_in_order() -> None:
    p = ScriptedPrompter(["x", True])
    assert p.text("a") == "x"
    assert p.confirm("b") is True
    assert p.asked == ["a", "b"]


def test_get_prompter_returns_a_prompter() -> None:
    assert get_prompter() is not None


# ── wizard stages ───────────────────────────────────────────────────────────
def test_choose_target_flag_wins_else_sole_local() -> None:
    assert wizard.choose_target(None, "gcp", False) == "gcp"      # flag as given
    assert wizard.choose_target(None, "", True) == "local"        # sole available, no prompt


def test_choose_step_flag_unknown_and_prompt() -> None:
    assert wizard.choose_step(None, "pdfs", False) is Step.PDFS
    with pytest.raises(StudioError, match="unknown step"):
        wizard.choose_step(None, "nope", False)
    with pytest.raises(StudioError, match="pass --step"):
        wizard.choose_step(None, "", False)                       # non-interactive, no flag
    assert wizard.choose_step(ScriptedPrompter(["export"]), "", True) is Step.EXPORT


def test_choose_pack_sole_pack_auto_and_flag() -> None:
    assert wizard.choose_pack(None, "", False) == "invoice"       # the only installed pack
    assert wizard.choose_pack(None, "contract", False) == "contract"


def test_build_generate_args_flags_bypass_prompts() -> None:
    a = wizard.build_generate_args(None, False, pack="invoice", run_id="r", total=3,
                                   catalogue="", fmt="pdf", condition="", date_from="", date_to="")
    assert isinstance(a, GenerateArgs) and a.run_id == "r" and a.total == 3


def test_build_generate_args_missing_required_is_an_error() -> None:
    with pytest.raises(StudioError, match="run-id"):
        wizard.build_generate_args(None, False, pack="invoice", run_id="", total=3,
                                   catalogue="", fmt="pdf", condition="", date_from="", date_to="")
    with pytest.raises(StudioError, match="total"):
        wizard.build_generate_args(None, False, pack="invoice", run_id="r", total=0,
                                   catalogue="", fmt="pdf", condition="", date_from="", date_to="")


def test_build_generate_args_prompts_when_interactive() -> None:
    p = ScriptedPrompter(["demo", "5", "", "clean", ""])   # run_id/total/catalogue/condition/from
    a = wizard.build_generate_args(p, True, pack="invoice", run_id="", total=0,
                                   catalogue="", fmt="pdf", condition="", date_from="", date_to="")
    assert a.run_id == "demo" and a.total == 5 and a.condition == "clean"


def test_build_catalogue_args_prompts_with_defaults() -> None:
    p = ScriptedPrompter(["v2", "50", "20", "procedural"])   # + provider-mix prompt
    a = wizard.build_catalogue_args(p, True, pack="invoice", version="v1", companies=1000,
                                    products_per_company=300, seed=0)
    assert a.version == "v2" and a.companies == 50 and a.products_per_company == 20
    assert a.mix == "procedural" and a.budget_usd == 0.0     # procedural never asks for a budget


def test_build_catalogue_args_llm_mix_then_prompts_budget() -> None:
    p = ScriptedPrompter(["v2", "50", "20", "cheap-mix", "12.5"])   # mix, then budget
    a = wizard.build_catalogue_args(p, True, pack="invoice", version="v1", companies=1000,
                                    products_per_company=300, seed=0)
    assert a.mix == "cheap-mix" and a.budget_usd == 12.5


def test_build_catalogue_args_mix_flag_bypasses_prompts() -> None:
    a = wizard.build_catalogue_args(None, False, pack="invoice", version="v1", companies=10,
                                    products_per_company=5, seed=0, mix="anthropic", budget_usd=7)
    assert a.mix == "anthropic" and a.budget_usd == 7.0


def test_choose_project_interactive_creates_when_none_saved(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    p = ScriptedPrompter([str(tmp_path / "ws")])          # just the workspace dir
    proj = wizard.choose_project(p, "local", LocalTarget(), reg, "",
                                 interactive=True, dry_run=False)
    assert (Path(proj.root) / "blobs").is_dir() and reg.get(proj.ref) is not None


def test_choose_project_interactive_picks_an_existing(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="local", id="ws", root=str(tmp_path / "ws"), last_run="old"))
    p = ScriptedPrompter(["ws"])                          # select the saved one by id
    proj = wizard.choose_project(p, "local", LocalTarget(), reg, "",
                                 interactive=True, dry_run=False)
    assert proj.last_run == "old"


# ── spinner ─────────────────────────────────────────────────────────────────
def test_run_with_spinner_returns_and_propagates() -> None:
    from docloom.studio.progress import run_with_spinner
    assert run_with_spinner("x", lambda: 42) == 42        # non-tty in pytest → direct call
    with pytest.raises(ValueError, match="boom"):
        run_with_spinner("x", lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_run_with_spinner_threaded_path(monkeypatch) -> None:
    import io
    import sys

    class _TTY(io.StringIO):
        def isatty(self) -> bool:
            return True
    monkeypatch.setattr(sys, "stderr", _TTY())
    from docloom.studio.progress import run_with_spinner
    assert run_with_spinner("work", lambda: 7) == 7        # exercises the worker thread


# ── exit option ─────────────────────────────────────────────────────────────
def test_choose_step_offers_exit_only_when_allowed() -> None:
    assert wizard.choose_step(ScriptedPrompter([EXIT]), "", True, allow_exit=True) == EXIT
    assert wizard.choose_step(ScriptedPrompter([BACK]), "", True, allow_back=True) == BACK
    assert wizard.choose_step(ScriptedPrompter(["pdfs"]), "", True, allow_exit=True) is Step.PDFS


# ── capture ─────────────────────────────────────────────────────────────────
def test_capture_puts_the_error_tail_in_detail(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.provision(ProjectSpec(target="local", id="ws", root=str(tmp_path / "ws")))
    r = t.run_export(p, ExportArgs(run_id="nope"), capture=True)   # no such run → exits non-zero
    assert not r.ok and r.detail                                    # stderr/stdout tail captured


# ── the interactive loop ────────────────────────────────────────────────────
class _FakeTarget:
    name = "local"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def normalise(self, spec: ProjectSpec):
        return Project(target="local", id=spec.id, root=spec.root)

    def provision(self, spec: ProjectSpec):
        return self.normalise(spec)

    def is_provisioned(self, project) -> bool:
        return True

    def _result(self, kind: str, args):
        self.calls.append(kind)
        return Result(ok=True, summary=f"fake {kind}", argv=(kind,),
                      run_id=getattr(args, "run_id", ""))

    def run_catalogue(self, p, a, *, dry_run=False, capture=False):
        return self._result("catalog", a)

    def run_generate(self, p, a, *, dry_run=False, capture=False):
        return self._result("pdfs", a)

    def run_export(self, p, a, *, dry_run=False, capture=False):
        return self._result("export", a)


def test_interactive_loop_runs_a_step_then_returns_and_exits(monkeypatch, tmp_path, capsys) -> None:
    from docloom import cli
    fake = _FakeTarget()
    monkeypatch.setenv("DOCLOOM_HOME", str(tmp_path / ".docloom"))
    monkeypatch.setattr("docloom.studio.prompts.is_interactive", lambda: True)
    # step=export, run_id, sink(blank), confirm=yes, then step=exit
    answers = ["export", "run-a", "", True, EXIT]
    monkeypatch.setattr("docloom.studio.prompts.get_prompter", lambda: ScriptedPrompter(answers))
    monkeypatch.setattr("docloom.studio.get_target", lambda name: fake)

    cli._run_studio(provider="local", project=str(tmp_path / "ws"))
    out = capsys.readouterr().out
    assert fake.calls.count("export") == 2         # preview (dry) + the real run
    assert "fake export" in out and "bye." in out   # ran the step, then exited on the menu


def test_generate_links_match_the_real_local_layout(tmp_path: Path) -> None:
    """Regression: local docs land at blobs/<run_id>/…, not blobs/runs/<run_id>/…."""
    t = LocalTarget()
    p = t.normalise(ProjectSpec(target="local", id="ws", root=str(tmp_path)))
    r = t.run_generate(p, GenerateArgs(run_id="rid", total=1), dry_run=True)
    docs = next(link.href for link in r.links if link.label == "documents")
    assert docs == str(tmp_path / "blobs" / "rid" / "documents")


def _drive(monkeypatch, tmp_path, answers, fake):
    from docloom import cli
    monkeypatch.setenv("DOCLOOM_HOME", str(tmp_path / ".docloom"))
    monkeypatch.setattr("docloom.studio.prompts.is_interactive", lambda: True)
    monkeypatch.setattr("docloom.studio.prompts.get_prompter", lambda: ScriptedPrompter(answers))
    monkeypatch.setattr("docloom.studio.get_target", lambda name: fake)
    cli._run_studio(provider="local", project=str(tmp_path / "ws"))


def test_backing_out_of_args_returns_to_the_step_menu(monkeypatch, tmp_path, capsys) -> None:
    fake = _FakeTarget()
    # pick catalog, then BACK at its first arg → step menu → exit. No run happens.
    _drive(monkeypatch, tmp_path, ["catalog", BACK, EXIT], fake)
    assert fake.calls == []                        # backed out before running
    assert "bye." in capsys.readouterr().out


def test_step_menu_back_returns_to_project_selection(monkeypatch, tmp_path, capsys) -> None:
    fake = _FakeTarget()
    # at the step menu pick BACK → project screen again → BACK there → leave.
    _drive(monkeypatch, tmp_path, [BACK, BACK], fake)
    assert fake.calls == []
    assert "bye." in capsys.readouterr().out


def test_no_provider_flag_shows_target_screen_and_back_returns_to_it(
        monkeypatch, tmp_path, capsys) -> None:
    """With no --provider, the studio opens on the target screen; ← back from the
    project screen returns to it (and exit there leaves)."""
    from docloom import cli
    fake = _FakeTarget()
    monkeypatch.setenv("DOCLOOM_HOME", str(tmp_path / ".docloom"))
    monkeypatch.setattr("docloom.studio.prompts.is_interactive", lambda: True)
    # target=local → project menu ← back (→ target screen) → target = exit
    monkeypatch.setattr("docloom.studio.prompts.get_prompter",
                        lambda: ScriptedPrompter(["local", BACK, EXIT]))
    monkeypatch.setattr("docloom.studio.get_target", lambda name: fake)
    cli._run_studio()                      # no provider ⇒ the target screen is the entry
    out = capsys.readouterr().out
    assert fake.calls == [] and "bye." in out


def test_no_provider_flag_non_interactive_defaults_to_local(tmp_path: Path) -> None:
    """Piped / CI with no --provider falls through to local — no prompt, scripts unchanged."""
    env = {"DOCLOOM_HOME": str(tmp_path / ".docloom")}
    res = runner.invoke(app, ["studio", "--project", str(tmp_path / "ws"),
                              "--step", "pdfs", "--run-id", "r", "--total", "1", "--dry-run"],
                        env=env)
    assert res.exit_code == 0, res.output
    assert "docloom generate" in res.output      # resolved to local, printed its plan


# ── progress + drain ────────────────────────────────────────────────────────
def test_pdfs_progress_is_none_for_non_pdfs(tmp_path: Path) -> None:
    from docloom.cli import _pdfs_progress
    proj = LocalTarget().provision(ProjectSpec(target="local", id="ws", root=str(tmp_path / "ws")))
    assert _pdfs_progress(proj, Step.CATALOG, CatalogueArgs()) is None
    poll = _pdfs_progress(proj, Step.PDFS, GenerateArgs(run_id="none", total=1))
    assert callable(poll) and poll() == ""         # no run yet → empty, never crashes


def test_drain_stdin_is_safe_without_a_tty() -> None:
    from docloom.studio.progress import drain_stdin
    drain_stdin()                                  # no TTY under pytest → a clean no-op


def test_spinner_accepts_a_progress_callback() -> None:
    from docloom.studio.progress import run_with_spinner
    assert run_with_spinner("x", lambda: 5, progress=lambda: "1/2") == 5


# ── gcp target (synthesis + dry-run; no gcloud) ─────────────────────────────
import re  # noqa: E402

import yaml  # noqa: E402


def _cfg_from(command: str) -> dict:
    path = re.search(r"-c (\S+)", command).group(1)
    return yaml.safe_load(Path(path).read_text())


def test_gcp_normalise_defaults_region_and_bucket() -> None:
    p = GcpTarget().normalise(ProjectSpec(target="gcp", id="acme-proj"))
    assert p.region == "us-central1" and p.bucket == "acme-proj-docloom"
    assert p.resources["state"].startswith("firestore://acme-proj")
    assert p.resources["storage"] == "gs://acme-proj-docloom/runs"


def test_gcp_catalogue_synthesises_config_and_links() -> None:
    t, p = GcpTarget(), GcpTarget().normalise(ProjectSpec(target="gcp", id="acme"))
    r = t.run_catalogue(p, CatalogueArgs(version="v2", companies=50), dry_run=True)
    assert "catalogue" in r.command and "deploy.sh" in r.command
    cfg = _cfg_from(r.command)
    assert cfg["project"] == "acme" and cfg["catalogue"]["version"] == "v2"
    assert cfg["catalogue"]["out"] == "gs://acme-docloom/catalogues/invoice/v2"
    assert any(link.href == "gs://acme-docloom/catalogues/invoice/v2" for link in r.links)
    assert any("console.cloud.google.com" in link.href for link in r.links)


def test_gcp_generate_is_deploy_then_dispatch_detached_one_slice() -> None:
    t = GcpTarget()
    p = t.normalise(ProjectSpec(target="gcp", id="acme"))
    r = t.run_generate(p, GenerateArgs(run_id="corpus1", total=5000, catalogue="gs://b/v2",
                                       condition="clean"), dry_run=True)
    # Detached: deploy then `dispatch` (no --wait), never the blocking `run`.
    assert " deploy " in r.command and "&&" in r.command
    assert r.command.rstrip().endswith(" dispatch")
    assert " run" not in r.command                 # not the blocking subcommand
    assert r.summary.startswith("dispatched")
    cfg = _cfg_from(r.command)
    assert cfg["run"]["id"] == "corpus1" and cfg["run"]["catalogue"] == "gs://b/v2"
    assert len(cfg["documents"]) == 1              # one slice → a clean run id
    assert cfg["documents"][0]["count"] == 5000
    assert cfg["documents"][0]["condition"] == "clean"
    docs = next(link.href for link in r.links if link.label == "documents")
    assert docs == "gs://acme-docloom/runs/corpus1/documents"
    assert r.run_id == "corpus1"


def test_gcp_export_defaults_to_bigquery() -> None:
    t = GcpTarget()
    p = t.normalise(ProjectSpec(target="gcp", id="acme"))
    r = t.run_export(p, ExportArgs(run_id="corpus1"), dry_run=True)
    cfg = _cfg_from(r.command)
    assert cfg["export"]["sink"] == "bigquery://acme/golden" and "export" in r.command
    assert any("bigquery" in link.href for link in r.links)


def test_gcp_studio_dry_run_via_the_command(tmp_path: Path) -> None:
    env = {"DOCLOOM_HOME": str(tmp_path / ".docloom")}
    argv = ["studio", "-p", "gcp", "--project", "acme", "--step", "catalog",
            "--version", "v2", "--dry-run"]
    res = runner.invoke(app, argv, env=env)
    assert res.exit_code == 0, res.output
    assert "deploy.sh" in res.output and "gcp:acme" in res.output


# ── provider mixes (LLM catalogue) ──────────────────────────────────────────
def test_mixes_procedural_is_keyless_and_llm_mixes_carry_secrets() -> None:
    from docloom.studio.mixes import get_mix, mix_names
    assert "procedural" in mix_names() and "cheap-mix" in mix_names()
    assert not get_mix("procedural").is_llm and get_mix("procedural").secrets_map() == {}
    cheap = get_mix("cheap-mix")
    assert cheap.is_llm and {p["name"] for p in cheap.providers_block()} == {
        "deepseek", "dashscope", "anthropic"}
    assert set(cheap.secrets_map()) == {
        "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"}
    with pytest.raises(StudioError, match="unknown provider mix"):
        get_mix("nope")


def test_gcp_catalogue_procedural_has_no_providers_block() -> None:
    t = GcpTarget()
    p = t.normalise(ProjectSpec(target="gcp", id="acme"))
    r = t.run_catalogue(p, CatalogueArgs(version="v2"), dry_run=True)   # default mix = procedural
    cfg = _cfg_from(r.command)["catalogue"]
    assert "providers" not in cfg and "secrets" not in cfg and "budget_usd" not in cfg


def test_gcp_catalogue_llm_mix_emits_providers_fallback_secrets_budget() -> None:
    t = GcpTarget()
    p = t.normalise(ProjectSpec(target="gcp", id="acme"))
    r = t.run_catalogue(p, CatalogueArgs(version="v2", mix="cheap-mix", budget_usd=25),
                        dry_run=True)
    cfg = _cfg_from(r.command)["catalogue"]
    assert {pr["name"] for pr in cfg["providers"]} == {"deepseek", "dashscope", "anthropic"}
    assert cfg["budget_usd"] == 25 and cfg["secrets"]["ANTHROPIC_API_KEY"] == "anthropic-api-key"
    assert cfg["fallback"][0]["name"] == "dashscope"     # quarantine leans on the cheap survivor
    assert "cheap-mix" in r.summary


def test_local_catalogue_llm_mix_writes_a_providers_file(tmp_path: Path) -> None:
    t = LocalTarget()
    p = t.provision(ProjectSpec(target="local", id="ws", root=str(tmp_path / "ws")))
    r = t.run_catalogue(p, CatalogueArgs(version="v1", mix="anthropic", budget_usd=5), dry_run=True)
    assert "--providers" in r.argv and "--budget-usd" in r.argv
    pf = Path(r.argv[r.argv.index("--providers") + 1])
    assert pf.is_file() and "anthropic" in pf.read_text()


def test_capture_catalogue_secrets_saves_names_never_values(tmp_path: Path) -> None:
    from docloom.cli import _capture_catalogue_secrets
    reg = Registry(tmp_path / "projects.yaml")
    proj = Project(target="gcp", id="acme")
    reg.add(proj)
    updated = _capture_catalogue_secrets(reg, proj, CatalogueArgs(mix="cheap-mix", budget_usd=25))
    assert set(updated.secrets_present) == {"deepseek-api-key", "dashscope-api-key",
                                            "anthropic-api-key"}
    assert reg.get("gcp:acme").secrets_present == updated.secrets_present   # persisted
    # a procedural mix records nothing
    assert _capture_catalogue_secrets(reg, proj, CatalogueArgs()).secrets_present == ()


def test_studio_catalog_llm_mix_dry_run_prints_secrets_and_plan(tmp_path: Path) -> None:
    home = tmp_path / ".docloom"
    Registry(home / "projects.yaml").add(
        Project(target="gcp", id="acme", region="us-central1", bucket="acme-docloom",
                resources={"state": "firestore://acme/(default)"}), make_default=True)
    res = runner.invoke(app, ["studio", "-p", "gcp", "--project", "acme", "--step", "catalog",
                              "--version", "v2", "--mix", "cheap-mix", "--budget", "25",
                              "--dry-run"],
                        env={"DOCLOOM_HOME": str(home)})
    assert res.exit_code == 0, res.output
    assert "Secret Manager secret" in res.output and "anthropic-api-key" in res.output
    assert "cheap-mix" in res.output


# ── onboarding an existing gcp project ──────────────────────────────────────
def test_gcp_adopt_registers_without_provisioning() -> None:
    p = GcpTarget().adopt(ProjectSpec(target="gcp", id="acme", region="us-east1", bucket="my-bkt"))
    assert p.provisioned_at and p.region == "us-east1" and p.bucket == "my-bkt"


def test_resolve_adopt_saves_a_gcp_project_without_gcloud(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "projects.yaml")
    p = resolve_project(reg, GcpTarget(), "gcp", "acme", adopt=True, region="us-east1", bucket="b1")
    assert reg.get("gcp:acme") is not None
    assert p.region == "us-east1" and p.bucket == "b1" and p.provisioned_at


def test_wizard_onboards_an_existing_gcp_project(tmp_path: Path) -> None:
    from docloom.studio.wizard import _ADOPT
    reg = Registry(tmp_path / "projects.yaml")
    answers = [_ADOPT, "acme", "us-west1", "acme-bucket"]     # onboard, id, region, bucket
    proj = wizard.choose_project(ScriptedPrompter(answers), "gcp", GcpTarget(), reg, "",
                                 interactive=True, dry_run=False)
    assert proj.region == "us-west1" and proj.bucket == "acme-bucket" and proj.provisioned_at
    assert reg.get("gcp:acme") is not None                    # saved, no gcloud touched


# ── detached dispatch: status / reattach ────────────────────────────────────
def test_status_of_builds_a_docloom_status_command() -> None:
    from docloom.studio.app import status_of
    proj = GcpTarget().normalise(ProjectSpec(target="gcp", id="acme"))
    r = status_of(proj, "corpus1", wait=True, dry_run=True)
    assert r.argv[:3] == ("status", "--run-id", "corpus1")
    assert r.argv[r.argv.index("--state") + 1].startswith("firestore://acme")
    assert "--wait" in r.argv and r.run_id == "corpus1"
    assert "docloom status --run-id corpus1" in r.command


def test_status_of_without_a_state_store_is_an_error() -> None:
    from docloom.studio.app import status_of
    with pytest.raises(StudioError, match="no recorded state"):
        status_of(Project(target="gcp", id="x"), "r", dry_run=True)    # no resources → no state


def test_resolve_saved_project_by_ref_id_default_and_missing(tmp_path: Path) -> None:
    from docloom.cli import _resolve_saved_project
    reg = Registry(tmp_path / "projects.yaml")
    reg.add(Project(target="gcp", id="acme", resources={"state": "firestore://acme/(default)"}),
            make_default=True)
    assert _resolve_saved_project(reg, "gcp:acme", "").id == "acme"      # explicit ref
    assert _resolve_saved_project(reg, "acme", "gcp").id == "acme"       # id + provider
    assert _resolve_saved_project(reg, "acme", "").id == "acme"          # unique bare id
    assert _resolve_saved_project(reg, "", "").ref == "gcp:acme"         # the default
    with pytest.raises(StudioError, match="no saved project"):
        _resolve_saved_project(reg, "gcp:nope", "")
    reg.add(Project(target="local", id="acme", root="/w"))              # now ambiguous
    with pytest.raises(StudioError, match="ambiguous"):
        _resolve_saved_project(reg, "acme", "")


def test_studio_status_command_dry_run_resolves_and_plans(tmp_path: Path) -> None:
    home = tmp_path / ".docloom"
    Registry(home / "projects.yaml").add(
        Project(target="gcp", id="acme", region="us-central1", bucket="acme-docloom",
                resources={"state": "firestore://acme/(default)"}), make_default=True)
    res = runner.invoke(app, ["studio", "status", "--project", "gcp:acme", "--run", "corpus1",
                              "--wait", "--dry-run"], env={"DOCLOOM_HOME": str(home)})
    assert res.exit_code == 0, res.output
    assert "docloom status --run-id corpus1" in res.output
    assert "firestore://acme" in res.output and "--wait" in res.output


def test_studio_status_unknown_project_is_a_clean_error(tmp_path: Path) -> None:
    res = runner.invoke(app, ["studio", "status", "--project", "gcp:none", "--run", "r"],
                        env={"DOCLOOM_HOME": str(tmp_path / ".docloom")})
    assert res.exit_code != 0 and "no saved project" in res.output


def test_studio_onboard_flag_threads_to_choose_project(monkeypatch, tmp_path: Path) -> None:
    """The --onboard/--region/--bucket flags reach choose_project as adopt + spec."""
    from docloom import cli
    seen = {}
    monkeypatch.setenv("DOCLOOM_HOME", str(tmp_path / ".docloom"))

    def fake_choose_project(prompter, provider, target, registry, project_flag, **kw):
        seen.update(adopt=kw.get("adopt"), region=kw.get("region"), bucket=kw.get("bucket"))
        return GcpTarget().adopt(ProjectSpec(
            target="gcp", id=project_flag,
            region=kw.get("region", ""), bucket=kw.get("bucket", "")))
    monkeypatch.setattr("docloom.studio.wizard.choose_project", fake_choose_project)
    cli._run_studio(provider="gcp", project="acme", onboard=True, region="eu", bucket="b",
                    step="export", run_id="r1", dry_run=True)
    assert seen == {"adopt": True, "region": "eu", "bucket": "b"}
