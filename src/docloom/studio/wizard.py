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
_ADOPT = "\x00adopt"


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
_TARGET_HINTS = {"local": "this machine — no cloud, no keys",
                 "gcp": "Cloud Run Jobs · GCS · Firestore"}


def choose_target(prompter: Prompter | None, provider_flag: str, interactive: bool,
                  *, allow_exit: bool = False) -> str:
    """The chosen target. A ``--provider`` flag wins outright; otherwise, in an
    interactive session with more than one target, this is the studio's first
    screen — pick local vs a cloud. Non-interactive (or a single target) falls
    through to the first target (``local``), so scripts need no flag. May return
    :data:`EXIT` when ``allow_exit`` and the operator chooses to leave."""
    if provider_flag:
        return provider_flag
    targets = available_targets()
    if len(targets) == 1 or not interactive or prompter is None:
        return targets[0]
    choices = [Choice(t, t, _TARGET_HINTS.get(t, "")) for t in targets]
    if allow_exit:
        choices.append(Choice(EXIT, "exit", "leave the studio"))
    return prompter.select("Deployment target", choices, default=targets[0])


def choose_project(
    prompter: Prompter | None, provider: str, target: DeploymentTarget, registry: Registry,
    project_flag: str, *, interactive: bool, dry_run: bool, allow_back: bool = False,
    adopt: bool = False, region: str = "", bucket: str = "",
) -> Project | str:
    """A saved project, a newly provisioned or onboarded one, or :data:`BACK`."""
    if project_flag:
        return resolve_project(registry, target, provider, project_flag, dry_run=dry_run,
                               adopt=adopt, region=region, bucket=bucket)
    if not (interactive and prompter is not None):
        return resolve_project(registry, target, provider, "", dry_run=dry_run)  # default or error

    cloud = provider != "local"
    saved = [p for p in registry.projects() if p.target == provider]
    choices = [Choice(p.id, p.id, p.root or p.bucket) for p in saved]
    choices.append(Choice(_NEW, "+ provision a new project" if cloud else "+ create a new project"))
    if cloud:
        choices.append(Choice(_ADOPT, "+ onboard an existing project"))
    if allow_back:
        choices.append(Choice(BACK, "← back"))
    default_ref = registry.default_ref()
    default = default_ref.split(":", 1)[1] if default_ref.startswith(f"{provider}:") else ""
    # Always show the menu for a cloud target so "onboard existing" is reachable.
    show = bool(saved) or allow_back or cloud
    picked = prompter.select("Project", choices, default=default) if show else _NEW
    if picked == BACK:
        return BACK
    if picked not in (_NEW, _ADOPT):
        return next(p for p in saved if p.id == picked)

    onboarding = picked == _ADOPT
    where = "Workspace directory" if provider == "local" else "GCP project id"
    name = prompter.text(where, default="./corpus" if provider == "local" else "",
                         allow_back=allow_back)
    if name == BACK:
        return BACK
    if not name:
        raise StudioError("a project name is required")
    if cloud:      # let an existing project point at its real region/bucket
        region = prompter.text("Region", default=region or "us-central1", allow_back=allow_back)
        if region == BACK:
            return BACK
        bucket = prompter.text("Bucket", default=bucket or f"{name}-docloom", allow_back=allow_back)
        if bucket == BACK:
            return BACK
    return resolve_project(registry, target, provider, name, dry_run=dry_run,
                           adopt=onboarding, region=region, bucket=bucket)


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
                         seed: int, mix: str = "procedural", budget_usd: float = 0.0,
                         concurrency: int = 8, tasks: int = 1) -> CatalogueArgs | str:
    from docloom.studio.mixes import get_mix, mix_names
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
        mix_choices = [Choice(n, n, get_mix(n).description) for n in mix_names()]
        mix_choices.append(Choice(BACK, "← back"))
        mix = prompter.select("LLM provider mix", mix_choices, default=mix)
        if mix == BACK:
            return BACK
    # A budget only bites an LLM build; the procedural pool spends nothing, so
    # neither prompt for nor carry a cap on it.
    resolved = get_mix(mix)
    if resolved.is_llm:
        if interactive and prompter is not None:
            raw = prompter.text("Budget (USD, hard cap, fleet-wide)",
                                default=f"{budget_usd or 20:g}", allow_back=True)
            if raw == BACK:
                return BACK
            budget_usd = float(raw) if raw.replace(".", "", 1).isdigit() else (budget_usd or 20.0)
    else:
        budget_usd = 0.0
    # concurrency/tasks are advanced tuning — taken from flags, never prompted.
    return CatalogueArgs(version=version, pack=pack, companies=companies,
                         products_per_company=products_per_company, seed=seed,
                         mix=mix, budget_usd=budget_usd,
                         concurrency=concurrency or 8, tasks=tasks or 1)


def build_generate_args(prompter: Prompter | None, interactive: bool, *, pack: str, run_id: str,
                        total: int, catalogue: str, fmt: str, condition: str, date_from: str,
                        date_to: str, selection_file: str = "", tasks: int = 4,
                        parallelism: int = 0) -> GenerateArgs | str:
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
                        condition=condition, date_from=date_from, date_to=date_to,
                        tasks=tasks or 4, parallelism=parallelism)


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
