"""The studio's vocabulary: targets, projects, steps, per-step args, results.

Deliberately small dataclasses with no behaviour — the wizard builds them from
prompts (or flags), a :class:`DeploymentTarget` consumes them, and a
:class:`Result` carries links back. Keeping them free of I/O is what lets the
whole flow be tested without a cloud account or a running generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class StudioError(Exception):
    """A studio-level problem stated for a human (unknown target, no project…)."""


class Step(StrEnum):
    """The three things the studio can run, mapped onto CLI verbs."""

    CATALOG = "catalog"     # build the content catalogue (docloom catalogue)
    PDFS = "pdfs"           # render documents + golden set (docloom generate)
    EXPORT = "export"       # golden shards → a queryable sink (docloom export)


@dataclass(frozen=True, slots=True)
class Link:
    """A named pointer shown after a step — a URL or a filesystem path."""

    label: str
    href: str


@dataclass(frozen=True, slots=True)
class Result:
    """What a step produced: a one-line summary, the command behind it, links."""

    ok: bool
    summary: str
    argv: tuple[str, ...] = ()      # the `docloom …` command run (or, on --dry-run, would run)
    links: tuple[Link, ...] = ()
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    """The minimum to name/create a project; a target fills in the rest."""

    target: str                     # "local" | "gcp" | …
    id: str                         # project id (gcp) or workspace name/path (local)
    region: str = ""
    bucket: str = ""
    root: str = ""                  # local workspace directory


@dataclass(frozen=True, slots=True)
class Project:
    """A provisioned environment on a target, as saved in the registry."""

    target: str
    id: str
    region: str = ""
    bucket: str = ""
    root: str = ""
    #: What provisioning created — for status and teardown. Cloud only.
    resources: dict[str, str] = field(default_factory=dict)
    #: Names of secrets known to be set — never the values (see registry).
    secrets_present: tuple[str, ...] = ()
    provisioned_at: str = ""
    created_at: str = ""
    last_run: str = ""

    @property
    def ref(self) -> str:
        """Stable handle, ``<target>:<id>`` — how a project is named on the CLI."""
        return f"{self.target}:{self.id}"


@dataclass(frozen=True, slots=True)
class CatalogueArgs:
    """`generate catalog` inputs. Phase 1 builds the procedural (key-free) pool;
    the LLM provider mix is a later, cloud-first concern."""

    version: str = "v1"
    pack: str = "invoice"
    companies: int = 1000
    products_per_company: int = 300
    seed: int = 0


@dataclass(frozen=True, slots=True)
class GenerateArgs:
    """`generate pdfs` inputs — a single slice inline, or a selection file."""

    run_id: str
    total: int
    pack: str = "invoice"
    fmt: str = "pdf"
    catalogue: str = ""             # catalogue uri/path; "" ⇒ the built-in seed pool
    selection_file: str = ""        # one slice's composition (from --config)
    condition: str = ""             # clean | light_scan | heavy_scan | handwritten
    date_from: str = ""             # issue-date window (YYYY-MM-DD)
    date_to: str = ""


@dataclass(frozen=True, slots=True)
class ExportArgs:
    """`export` inputs — a run and a sink; "" sink ⇒ the target's default."""

    run_id: str
    sink: str = ""


class DeploymentTarget(Protocol):
    """Where a run executes. ``local`` and ``gcp`` ship; ``aws``/``azure`` later.

    The registry owns *persistence* of projects; a target owns *provisioning* and
    *running*. Each ``run_*`` returns a :class:`Result` and honours ``dry_run`` by
    resolving the command without executing it.
    """

    name: str

    def normalise(self, spec: ProjectSpec) -> Project: ...      # fill defaults, no I/O (dry-run)
    def provision(self, spec: ProjectSpec) -> Project: ...      # create resources, return saved
    def is_provisioned(self, project: Project) -> bool: ...

    def run_catalogue(
        self, project: Project, args: CatalogueArgs, *, dry_run: bool = False) -> Result: ...

    def run_generate(
        self, project: Project, args: GenerateArgs, *, dry_run: bool = False) -> Result: ...

    def run_export(
        self, project: Project, args: ExportArgs, *, dry_run: bool = False) -> Result: ...
