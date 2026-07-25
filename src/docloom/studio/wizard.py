"""The wizard — resolve each stage from a flag if given, else a prompt.

`target → project → pack → step → args`. Every stage takes the flag value first
and only prompts for what is missing, so the same code serves a fully-flagged
(non-interactive) run and a bare interactive walk-through. Interactive prompts can
return :data:`BACK` (the operator chose "go back") or, at the step menu,
:data:`EXIT`; the command driver navigates on those. Reuses phase-1 plumbing:
:func:`resolve_project`, the target adapters, the registry.
"""
from __future__ import annotations

from docloom.core import available_packs
from docloom.studio.app import resolve_project
from docloom.studio.prompts import BACK, EXIT, Choice, Prompter
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

_NEW = "\x00new"


# ── small resolvers ─────────────────────────────────────────────────────────
def _required_text(value: str, interactive: bool, prompter: Prompter | None,
                   message: str, what: str, *, allow_back: bool = False) -> str:
    if value:
        return value
    if not interactive or prompter is None:
        raise StudioError(f"missing --{what}; pass it or run interactively")
    answer = prompter.text(message, allow_back=allow_back)
    if answer == BACK:
        return BACK
    if answer:
        return answer
    raise StudioError(f"{what} is required")


def _required_int(value: int, interactive: bool, prompter: Prompter | None,
                  message: str, what: str, *, allow_back: bool = False) -> int | str:
    if value and value > 0:
        return value
    if not interactive or prompter is None:
        raise StudioError(f"missing --{what}; pass a positive integer or run interactively")
    raw = prompter.text(message, allow_back=allow_back)
    if raw == BACK:
        return BACK
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
    project_flag: str, *, interactive: bool, dry_run: bool, allow_back: bool = False,
) -> Project | str:
    """A saved project, a freshly created one, or :data:`BACK`."""
    if project_flag:
        return resolve_project(registry, target, provider, project_flag, dry_run=dry_run)
    if not (interactive and prompter is not None):
        return resolve_project(registry, target, provider, "", dry_run=dry_run)  # default or error

    saved = [p for p in registry.projects() if p.target == provider]
    choices = [Choice(p.id, p.id, p.root or p.bucket) for p in saved]
    choices.append(Choice(_NEW, "+ create a new project"))
    if allow_back:
        choices.append(Choice(BACK, "← back"))
    default_ref = registry.default_ref()
    default = default_ref.split(":", 1)[1] if default_ref.startswith(f"{provider}:") else ""
    picked = prompter.select("Project", choices, default=default) if (saved or allow_back) else _NEW
    if picked == BACK:
        return BACK
    if picked != _NEW:
        return next(p for p in saved if p.id == picked)

    where = "Workspace directory" if provider == "local" else "Project id"
    name = prompter.text(where, default="./corpus" if provider == "local" else "",
                         allow_back=allow_back)
    if name == BACK:
        return BACK
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


def choose_step(prompter: Prompter | None, step_flag: str, interactive: bool,
                *, allow_exit: bool = False, allow_back: bool = False) -> Step | str:
    """The chosen :class:`Step`, or :data:`BACK` / :data:`EXIT` (offered only when
    ``allow_back`` / ``allow_exit``)."""
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
    if allow_back:
        choices.append(Choice(BACK, "← back", "choose another project"))
    if allow_exit:
        choices.append(Choice(EXIT, "exit", "leave the studio"))
    picked = prompter.select("Step", choices)
    if picked in (BACK, EXIT):
        return picked
    return Step(picked)


# ── per-step args ───────────────────────────────────────────────────────────
# Each builder returns its args dataclass, or BACK if the operator backed out of
# any prompt (the driver then returns to the step menu).
def build_catalogue_args(prompter: Prompter | None, interactive: bool, *, pack: str,
                         version: str, companies: int, products_per_company: int,
                         seed: int) -> CatalogueArgs | str:
    if interactive and prompter is not None:
        version = prompter.text("Catalogue version", default=version, allow_back=True)
        if version == BACK:
            return BACK
        raw = prompter.text("Companies", default=str(companies), allow_back=True)
        if raw == BACK:
            return BACK
        companies = _as_int(raw, companies)
        raw = prompter.text("Products per company", default=str(products_per_company),
                            allow_back=True)
        if raw == BACK:
            return BACK
        products_per_company = _as_int(raw, products_per_company)
    return CatalogueArgs(version=version, pack=pack, companies=companies,
                         products_per_company=products_per_company, seed=seed)


def build_generate_args(prompter: Prompter | None, interactive: bool, *, pack: str, run_id: str,
                        total: int, catalogue: str, fmt: str, condition: str, date_from: str,
                        date_to: str, selection_file: str = "") -> GenerateArgs | str:
    run_id = _required_text(run_id, interactive, prompter, "Run id", "run-id", allow_back=True)
    if run_id == BACK:
        return BACK
    total = _required_int(total, interactive, prompter, "How many documents", "total",
                          allow_back=True)
    if total == BACK:
        return BACK
    if interactive and prompter is not None:
        catalogue = prompter.text("Catalogue uri (blank = built-in seed pool)",
                                  default=catalogue, allow_back=True)
        if catalogue == BACK:
            return BACK
        if not selection_file:      # a slice file supplies its own composition — don't double-ask
            condition = prompter.text("Condition clean|light_scan|heavy_scan|handwritten"
                                      " (blank=clean)", default=condition, allow_back=True)
            if condition == BACK:
                return BACK
            date_from = prompter.text("Issue date from YYYY-MM-DD (blank = default window)",
                                      default=date_from, allow_back=True)
            if date_from == BACK:
                return BACK
            if date_from:
                date_to = prompter.text("Issue date to YYYY-MM-DD", default=date_to,
                                        allow_back=True)
                if date_to == BACK:
                    return BACK
    return GenerateArgs(run_id=run_id, total=total, pack=pack, fmt=fmt,  # type: ignore[arg-type]
                        catalogue=catalogue, selection_file=selection_file,
                        condition=condition, date_from=date_from, date_to=date_to)


def build_export_args(prompter: Prompter | None, interactive: bool, *,
                      run_id: str, sink: str) -> ExportArgs | str:
    run_id = _required_text(run_id, interactive, prompter, "Run id to export", "run-id",
                            allow_back=True)
    if run_id == BACK:
        return BACK
    if interactive and prompter is not None:
        sink = prompter.text("Sink uri (blank = a local DuckDB file)", default=sink,
                             allow_back=True)
        if sink == BACK:
            return BACK
    return ExportArgs(run_id=run_id, sink=sink)
