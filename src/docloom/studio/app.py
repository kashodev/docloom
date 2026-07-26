"""Studio orchestration — resolve a project, dispatch a step.

Pure of I/O beyond the registry and the target, so the CLI command on top is a
thin layer of prompts/printing. This is where the flag-driven phase-1 flow and
the later wizard both converge.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from docloom.studio.registry import Registry
from docloom.studio.types import (
    DeploymentTarget,
    Project,
    ProjectSpec,
    Result,
    Step,
    StudioError,
)


def spec_for(provider: str, project: str, *, region: str = "", bucket: str = "") -> ProjectSpec:
    """A :class:`ProjectSpec` from the ``--project`` value. For ``local`` the value
    doubles as the workspace directory; for a cloud target it is the project id,
    with optional ``region``/``bucket`` (for onboarding an existing project)."""
    if provider == "local":
        name = Path(project).name or project
        return ProjectSpec(target="local", id=name, root=project)
    return ProjectSpec(target=provider, id=project, region=region, bucket=bucket)


def resolve_project(
    registry: Registry, target: DeploymentTarget, provider: str, project: str,
    *, dry_run: bool = False, adopt: bool = False, region: str = "", bucket: str = "",
) -> Project:
    """The saved project named by ``--project``, creating it if new.

    Adopt-and-fill: a ``--project`` that already exists is reused, never an error.
    A new one is **provisioned** (resources created) and saved, or, with ``adopt``,
    **onboarded** — registered as an already-set-up environment without creating
    resources. ``dry_run`` resolves it without touching the disk or the registry.
    """
    if project:
        spec = spec_for(provider, project, region=region, bucket=bucket)
        normalised = target.normalise(spec)
        if (existing := registry.get(normalised.ref)) is not None:
            return existing
        if dry_run:
            return normalised
        created = target.adopt(spec) if adopt else target.provision(spec)
        registry.add(created)
        return created

    ref = registry.default_ref()
    saved = registry.get(ref) if ref else None
    if saved is None or saved.target != provider:
        raise StudioError(
            f"no project selected for target {provider!r}. Pass --project <name> "
            "(a new name is created and saved)."
        )
    return saved


def status_of(project: Project, run_id: str, *, wait: bool = False,
              dry_run: bool = False, capture: bool = False) -> Result:
    """Reattach to a run and report its progress — the reader half of detached
    dispatch. A cloud run (`deploy.sh dispatch`) reports into the project's state
    store, so this reads that store directly with ``docloom status`` rather than
    holding a job open; ``wait`` streams until the run finishes. It is
    target-agnostic: the state URI (``firestore://…`` for gcp, a SQLite path for
    local) comes from the saved project, so one path serves every target."""
    state_uri = project.resources.get("state")
    if not state_uri:
        raise StudioError(
            f"project {project.ref} has no recorded state store to read a run from "
            "— re-provision or onboard it so its state URI is saved.")
    argv = ["status", "--run-id", run_id, "--state", state_uri]
    if wait:
        argv.append("--wait")
    command = "docloom " + " ".join(argv)
    summary = f"status {run_id}" + (" (streaming until done)" if wait else "")
    if dry_run:
        return Result(ok=True, summary=summary, argv=tuple(argv), command=command, run_id=run_id)
    proc = subprocess.run([sys.executable, "-m", "docloom", *argv],
                          check=False, capture_output=capture, text=capture)
    ok = proc.returncode == 0
    detail = ""
    if capture:
        tail = (proc.stdout or "").strip().splitlines()[-12:]
        if not ok and not tail:
            tail = (proc.stderr or "").strip().splitlines()[-12:]
        detail = "\n".join(tail)
    return Result(ok=ok, summary=summary if ok else f"failed: {summary}",
                  argv=tuple(argv), command=command, run_id=run_id, detail=detail)


def run_step(
    target: DeploymentTarget, project: Project, step: Step, args: object,
    *, dry_run: bool = False, capture: bool = False,
) -> Result:
    if step is Step.CATALOG:
        return target.run_catalogue(project, args, dry_run=dry_run, capture=capture)  # type: ignore[arg-type]
    if step is Step.PDFS:
        return target.run_generate(project, args, dry_run=dry_run, capture=capture)   # type: ignore[arg-type]
    if step is Step.EXPORT:
        return target.run_export(project, args, dry_run=dry_run, capture=capture)     # type: ignore[arg-type]
    raise StudioError(f"unknown step {step!r}")
