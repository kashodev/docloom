"""The ``gcp`` deployment target — Cloud Run Jobs, via the tested ``deploy.sh``.

Rather than re-implement provisioning and job orchestration, this synthesises the
``run.yaml`` that ``deploy/gcp/deploy.sh`` already understands and shells into it:
``provision`` + ``build`` to stand a project up, ``catalogue`` / ``deploy`` +
``run`` / ``export`` for the three steps. So the studio reuses one source of truth
for the GCP wiring, and a run is the same whether launched from the studio or by
hand. ``dry_run`` writes the config and returns the command without executing —
which is also how the whole surface is tested, no ``gcloud`` required.

Links are built from the known layout: documents at ``gs://<bucket>/runs/<run>/``,
state in Firestore, plus Cloud Console URLs.

Requires a docloom checkout (``deploy.sh`` is not shipped in the wheel yet) and an
authenticated ``gcloud``; a missing script or a failed call surfaces as a clear
error, never a crash.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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
    StudioError,
)

_CONSOLE = "https://console.cloud.google.com"


def _deploy_script() -> Path:
    script = Path(__file__).resolve().parents[3] / "deploy" / "gcp" / "deploy.sh"
    if not script.is_file():
        raise StudioError(
            "deploy.sh not found — the gcp target needs a docloom checkout for now "
            f"(looked in {script.parent})"
        )
    return script


def _bucket_url(bucket: str, path: str, project: str) -> str:
    return f"{_CONSOLE}/storage/browser/{bucket}/{path}?project={project}"


class GcpTarget:
    name = "gcp"

    # ── onboarding ──────────────────────────────────────────────────────────
    def normalise(self, spec: ProjectSpec) -> Project:
        region = spec.region or "us-central1"
        bucket = spec.bucket or f"{spec.id}-docloom"
        return Project(
            target=self.name, id=spec.id, region=region, bucket=bucket,
            resources={"storage": f"gs://{bucket}/runs",
                       "state": f"firestore://{spec.id}/(default)"},
        )

    def is_provisioned(self, project: Project) -> bool:
        return bool(project.provisioned_at)   # best effort; we don't probe gcloud here

    def provision(self, spec: ProjectSpec) -> Project:
        """Stand the project up: APIs, bucket, Firestore + claim index, service
        account, then build the image so a later `deploy` has something to run."""
        project = self.normalise(spec)
        cfg = self._write_config(self._base_config(project))
        self._sh(cfg, ["provision", "build"], capture=False)
        return replace(project, provisioned_at=now_iso())

    # ── steps ───────────────────────────────────────────────────────────────
    def run_catalogue(self, project: Project, args: CatalogueArgs, *,
                      dry_run: bool = False, capture: bool = False) -> Result:
        out = f"gs://{project.bucket}/catalogues/{args.pack}/{args.version}"
        config = self._base_config(project) | {
            "catalogue": {"out": out, "version": args.version, "companies": args.companies,
                          "products_per_company": args.products_per_company}
        }   # no providers ⇒ the procedural build; an LLM mix is a later addition
        links = (
            Link("catalogue", out),
            Link("console", _bucket_url(project.bucket,
                                        f"catalogues/{args.pack}/{args.version}", project.id)),
        )
        return self._run(config, ["catalogue"], dry_run, capture,
                         summary=f"catalogue {args.version} → {out}", links=links)

    def run_generate(self, project: Project, args: GenerateArgs, *,
                     dry_run: bool = False, capture: bool = False) -> Result:
        run: dict[str, object] = {"id": args.run_id, "total": args.total, "format": args.fmt}
        if args.catalogue:
            run["catalogue"] = args.catalogue
        # A single slice means deploy.sh's run id is exactly run.id (no suffix).
        slice_: dict[str, object] = {"name": "docs", "count": args.total}
        if args.condition:
            slice_["condition"] = args.condition
        if args.date_from and args.date_to:
            slice_["date_range"] = [args.date_from, args.date_to]
        config = self._base_config(project) | {"run": run, "documents": [slice_]}
        base = f"gs://{project.bucket}/runs/{args.run_id}"
        links = (
            Link("documents", f"{base}/documents"),
            Link("golden", f"{base}/golden"),
            Link("console", _bucket_url(project.bucket, f"runs/{args.run_id}", project.id)),
            Link("job", f"{_CONSOLE}/run/jobs?project={project.id}&region={project.region}"),
        )
        return self._run(config, ["deploy", "run"], dry_run, capture, run_id=args.run_id,
                         summary=f"generate {args.total} {args.pack} → {base}", links=links)

    def run_export(self, project: Project, args: ExportArgs, *,
                   dry_run: bool = False, capture: bool = False) -> Result:
        sink = args.sink or f"bigquery://{project.id}/golden"
        config = self._base_config(project) | {"run": {"id": args.run_id}, "export": {"sink": sink}}
        links = (
            Link("sink", sink),
            Link("console", f"{_CONSOLE}/bigquery?project={project.id}"
                            if sink.startswith("bigquery") else
                            _bucket_url(project.bucket, f"runs/{args.run_id}", project.id)),
        )
        return self._run(config, ["export"], dry_run, capture, run_id=args.run_id,
                         summary=f"export {args.run_id} → {sink}", links=links)

    # ── internals ───────────────────────────────────────────────────────────
    def _base_config(self, project: Project) -> dict:
        return {
            "project": project.id,
            "region": project.region,
            "bucket": project.bucket,
            "firestore": {"database": "(default)", "location": "nam5"},
            "artifact_registry": {"repo": "docloom", "image_tag": "v1"},
            "job": {"name": "docloom-generate", "service_account": "docloom-run",
                    "tasks": 4, "parallelism": 4, "cpu": 2, "memory": "4Gi"},
        }

    def _write_config(self, config: dict) -> Path:
        import yaml
        fd, path = tempfile.mkstemp(suffix=".studio.yaml")
        with os.fdopen(fd, "w") as fh:
            fh.write(yaml.safe_dump(config, sort_keys=False))
        return Path(path)

    def _display(self, cfg: Path, subcommands: list[str]) -> str:
        script = "deploy/gcp/deploy.sh"
        return "  &&  ".join(f"PYTHON={sys.executable} ./{script} -c {cfg} {sub}"
                             for sub in subcommands)

    def _run(self, config: dict, subcommands: list[str], dry_run: bool, capture: bool, *,
             summary: str, links: tuple[Link, ...] = (), run_id: str = "") -> Result:
        cfg = self._write_config(config)
        command = self._display(cfg, subcommands)
        if dry_run:
            return Result(ok=True, summary=summary, command=command, links=links, run_id=run_id)
        ok, detail = self._sh(cfg, subcommands, capture=capture)
        return Result(
            ok=ok, summary=summary if ok else f"failed: {summary}", command=command,
            links=links if ok else (), run_id=run_id, detail=detail,
        )

    def _sh(self, cfg: Path, subcommands: list[str], *, capture: bool) -> tuple[bool, str]:
        """Run each ``deploy.sh -c <cfg> <sub>`` in order; stop at the first failure."""
        script = _deploy_script()
        env = {**os.environ, "PYTHON": sys.executable}
        for sub in subcommands:
            proc = subprocess.run(
                [str(script), "-c", str(cfg), sub], cwd=str(script.parent), env=env,
                check=False, capture_output=capture, text=capture,
            )
            if proc.returncode != 0:
                tail = ((proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
                        if capture else [])
                return False, "\n".join(tail)
        return True, ""
