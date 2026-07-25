"""The wizard — resolve each stage from a flag if given, else a prompt.

`target → project → pack → step → args`. Every stage takes the flag value first
and only prompts for what is missing, so the same code serves a fully-flagged
(non-interactive) run and a bare interactive walk-through. It reuses phase-1
plumbing: :func:`resolve_project`, the target adapters, the registry.
"""
from __future__ import annotations

from docloom.core import available_packs
from docloom.studio.app import resolve_project
from docloom.studio.prompts import Choice, Prompter
from docloom.studio.registry import Registry
from docloom.studio.targets import available_targets
from docloom.studio.types import (
    CatalogueArgs,
    DeploymentTarget,
    ExportArgs,
    GenerateArgs,
    Project,
    Step,
    StudioError,
)


# ── small resolvers ─────────────────────────────────────────────────────────
def _required_text(value: str, interactive: bool, prompter: Prompter | None,
                   message: str, what: str) -> str:
    if value:
        return value
    if not interactive or prompter is None:
        raise StudioError(f"missing --{what}; pass it or run interactively")
    if answer := prompter.text(message):
        return answer
    raise StudioError(f"{what} is required")


def _required_int(value: int, interactive: bool, prompter: Prompter | None,
                  message: str, what: str) -> int:
    if value and value > 0:
        return value
    if not interactive or prompter is None:
        raise StudioError(f"missing --{what}; pass a positive integer or run interactively")
    raw = prompter.text(message)
    if not (raw.isdigit() and int(raw) > 0):
        raise StudioError(f"{what} must be a positive integer")
    return int(raw)


def _as_int(raw: str, fallback: int) -> int:
    return int(raw) if raw.isdigit() else fallback


# ── stages ──────────────────────────────────────────────────────────────────
def choose_target(prompter: Prompter | None, provider_flag: str, interactive: bool) -> str:
    if provider_flag:
        return provider_flag
    targets = available_targets()
    if len(targets) == 1 or not interactive or prompter is None:
        return targets[0]
    choices = [Choice(t, t) for t in targets]
    return prompter.select("Deployment target", choices, default=targets[0])


def choose_project(
    prompter: Prompter | None, provider: str, target: DeploymentTarget, registry: Registry,
    project_flag: str, *, interactive: bool, dry_run: bool,
) -> Project:
    if project_flag:
        return resolve_project(registry, target, provider, project_flag, dry_run=dry_run)
    if not (interactive and prompter is not None):
        return resolve_project(registry, target, provider, "", dry_run=dry_run)  # default or error

    saved = [p for p in registry.projects() if p.target == provider]
    choices = [Choice(p.id, p.id, p.root or p.bucket) for p in saved]
    choices.append(Choice("\x00new", "+ create a new project"))
    default_ref = registry.default_ref()
    default = default_ref.split(":", 1)[1] if default_ref.startswith(f"{provider}:") else ""
    picked = prompter.select("Project", choices, default=default) if saved else "\x00new"
    if picked != "\x00new":
        return next(p for p in saved if p.id == picked)

    where = "Workspace directory" if provider == "local" else "Project id"
    name = prompter.text(where, default="./corpus" if provider == "local" else "")
    if not name:
        raise StudioError("a project name is required")
    return resolve_project(registry, target, provider, name, dry_run=dry_run)


def choose_pack(prompter: Prompter | None, pack_flag: str, interactive: bool) -> str:
    if pack_flag:
        return pack_flag
    packs = tuple(available_packs()) or ("invoice",)
    if len(packs) == 1 or not (interactive and prompter is not None):
        return packs[0]
    choices = [Choice(p, p) for p in packs]
    return prompter.select("Document type (pack)", choices, default=packs[0])


def choose_step(prompter: Prompter | None, step_flag: str, interactive: bool) -> Step:
    if step_flag:
        try:
            return Step(step_flag)
        except ValueError:
            raise StudioError(f"unknown step {step_flag!r}; use catalog | pdfs | export") from None
    if not (interactive and prompter is not None):
        raise StudioError("pass --step catalog|pdfs|export")
    choices = [
        Choice("catalog", "generate catalog", "build the product catalogue"),
        Choice("pdfs", "generate pdfs", "render documents + the golden set"),
        Choice("export", "export", "golden shards → a queryable sink"),
    ]
    return Step(prompter.select("Step", choices))


# ── per-step args ───────────────────────────────────────────────────────────
def build_catalogue_args(prompter: Prompter | None, interactive: bool, *, pack: str, version: str,
                         companies: int, products_per_company: int, seed: int) -> CatalogueArgs:
    if interactive and prompter is not None:
        version = prompter.text("Catalogue version", default=version) or version
        companies = _as_int(prompter.text("Companies", default=str(companies)), companies)
        products_per_company = _as_int(
            prompter.text("Products per company", default=str(products_per_company)),
            products_per_company)
    return CatalogueArgs(version=version, pack=pack, companies=companies,
                         products_per_company=products_per_company, seed=seed)


def build_generate_args(prompter: Prompter | None, interactive: bool, *, pack: str, run_id: str,
                        total: int, catalogue: str, fmt: str, condition: str, date_from: str,
                        date_to: str, selection_file: str = "") -> GenerateArgs:
    run_id = _required_text(run_id, interactive, prompter, "Run id", "run-id")
    total = _required_int(total, interactive, prompter, "How many documents", "total")
    if interactive and prompter is not None:
        catalogue = prompter.text("Catalogue uri (blank = built-in seed pool)", default=catalogue)
        if not selection_file:      # a slice file supplies its own composition — don't double-ask
            condition = prompter.text(
                "Condition clean|light_scan|heavy_scan|handwritten (blank=clean)",
                default=condition)
            date_from = prompter.text(
                "Issue date from YYYY-MM-DD (blank = default window)", default=date_from)
            if date_from:
                date_to = prompter.text("Issue date to YYYY-MM-DD", default=date_to)
    return GenerateArgs(run_id=run_id, total=total, pack=pack, fmt=fmt, catalogue=catalogue,
                        selection_file=selection_file, condition=condition,
                        date_from=date_from, date_to=date_to)


def build_export_args(prompter: Prompter | None, interactive: bool, *,
                      run_id: str, sink: str) -> ExportArgs:
    run_id = _required_text(run_id, interactive, prompter, "Run id to export", "run-id")  # type: ignore[arg-type]
    if interactive and prompter is not None:
        sink = prompter.text("Sink uri (blank = a local DuckDB file)", default=sink)
    return ExportArgs(run_id=run_id, sink=sink)
