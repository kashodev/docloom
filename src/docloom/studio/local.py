"""The ``local`` deployment target — generate on this machine, no cloud, no keys.

Every step becomes a ``python -m docloom …`` invocation against a workspace
directory (``file://`` storage, a SQLite state file, a DuckDB sink), so the
studio reuses the exact CLI a hand-run would. Running through ``python -m`` (not a
bare ``docloom``) means it works in a dev checkout and once installed alike.
``dry_run`` resolves the command and links without executing — the whole surface
is testable without rendering a single PDF.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from docloom.studio.registry import now_iso
from docloom.studio.types import (
    CatalogueArgs,
    ExportArgs,
    GenerateArgs,
    Link,
    Project,
    ProjectSpec,
    Result,
)


class LocalTarget:
    name = "local"

    # ── onboarding ──────────────────────────────────────────────────────────
    def normalise(self, spec: ProjectSpec) -> Project:
        """A workspace ``Project`` from a spec — resolves the root, no I/O."""
        root = Path(spec.root or spec.id).expanduser().resolve()
        return Project(
            target=self.name,
            id=spec.id or root.name,
            root=str(root),
            resources={"storage": str(root / "blobs"), "state": str(root / "runs.db")},
        )

    def provision(self, spec: ProjectSpec) -> Project:
        """'Provisioning' is just a workspace: blobs/ for documents + golden
        shards, and a SQLite state file alongside. Idempotent — existing dirs are
        left as they are."""
        project = self.normalise(spec)
        (Path(project.root) / "blobs").mkdir(parents=True, exist_ok=True)
        return replace(project, created_at=now_iso())

    def adopt(self, spec: ProjectSpec) -> Project:
        """A local workspace has nothing to "already exist" remotely, so onboarding
        one is the same as provisioning it — ensure its directories."""
        return self.provision(spec)

    def is_provisioned(self, project: Project) -> bool:
        return bool(project.root) and (Path(project.root) / "blobs").is_dir()

    # ── steps ───────────────────────────────────────────────────────────────
    def run_catalogue(self, project: Project, args: CatalogueArgs, *,
                      dry_run: bool = False, capture: bool = False) -> Result:
        out = str(Path(project.root, "catalogues", args.pack, args.version))
        argv = [
            "catalogue", "--out", out, "--version", args.version,
            "--pack", args.pack, "--companies", str(args.companies),
            "--products-per-company", str(args.products_per_company),
            "--seed", str(args.seed),
        ]   # no --providers => the procedural, key-free build
        return self._invoke(
            argv, dry_run, capture=capture,
            summary=f"catalogue {args.version} · {args.companies} companies x "
                    f"{args.products_per_company} (procedural, no keys)",
            links=(Link("catalogue", out),),
        )

    def run_generate(self, project: Project, args: GenerateArgs, *,
                     dry_run: bool = False, capture: bool = False) -> Result:
        argv = [
            "generate", "--run-id", args.run_id, "--total", str(args.total),
            "--pack", args.pack, "--format", args.fmt,
            "--storage", self._storage(project), "--state", self._state(project),
        ]
        if args.catalogue:
            argv += ["--catalogue", args.catalogue]
        if args.selection_file:
            argv += ["--selection-file", args.selection_file]
        if args.condition:
            argv += ["--condition", args.condition]
        if args.date_from and args.date_to:
            argv += ["--issue-date-from", args.date_from, "--issue-date-to", args.date_to]
        run_dir = Path(project.root, "blobs", args.run_id)
        docs = run_dir / "documents"
        ext = "pdf" if args.fmt == "pdf" else "html"
        return self._invoke(
            argv, dry_run, capture=capture,
            summary=f"generate {args.total} {args.pack} ({args.fmt}) → {project.root}",
            links=(
                Link("documents", str(docs)),
                Link("golden", str(run_dir / "golden")),
                Link("open", f"open {docs}/unit-000000/inv_00000000.{ext}"),
            ),
            run_id=args.run_id,
        )

    def run_export(self, project: Project, args: ExportArgs, *,
                   dry_run: bool = False, capture: bool = False) -> Result:
        sink = args.sink or f"duckdb://{Path(project.root, 'corpus.duckdb')}"
        argv = [
            "export", "--run-id", args.run_id, "--sink", sink,
            "--storage", self._storage(project),
        ]
        return self._invoke(
            argv, dry_run, capture=capture,
            summary=f"export {args.run_id} → {sink}",
            links=(Link("sink", sink),),
            run_id=args.run_id,
        )

    # ── internals ───────────────────────────────────────────────────────────
    def _storage(self, project: Project) -> str:
        return str(Path(project.root, "blobs"))

    def _state(self, project: Project) -> str:
        return str(Path(project.root, "runs.db"))

    def _invoke(self, argv: list[str], dry_run: bool, *, summary: str,
                links: tuple[Link, ...] = (), run_id: str = "", capture: bool = False) -> Result:
        command = "docloom " + " ".join(argv)
        if dry_run:
            return Result(ok=True, summary=summary, argv=tuple(argv), command=command,
                          links=links, run_id=run_id)
        # Capture only when a spinner is covering the run (interactive), so the
        # animation stays clean; otherwise inherit the terminal and stream logs.
        proc = subprocess.run([sys.executable, "-m", "docloom", *argv],
                              check=False, capture_output=capture, text=capture)
        ok = proc.returncode == 0
        detail = ""
        if not ok and capture:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            detail = "\n".join(tail)
        return Result(
            ok=ok,
            summary=summary if ok else f"failed (exit {proc.returncode}): {summary}",
            argv=tuple(argv), command=command, links=links if ok else (),
            run_id=run_id, detail=detail,
        )
