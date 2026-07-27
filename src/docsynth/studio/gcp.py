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

Requires a docsynth checkout (``deploy.sh`` is not shipped in the wheel yet) and an
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

from docsynth.studio.registry import now_iso
from docsynth.studio.types import (
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
            "deploy.sh not found — the gcp target needs a docsynth checkout for now "
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
        bucket = spec.bucket or f"{spec.id}-docsynth"
        return Project(
            target=self.name, id=spec.id, region=region, bucket=bucket,
            resources={"storage": f"gs://{bucket}/runs",
                       "state": f"firestore://{spec.id}/(default)"},
        )

    def adopt(self, spec: ProjectSpec) -> Project:
        """Onboard an already-set-up GCP project: register it, create nothing. The
        operator asserts it exists (docsynth-provisioned, or they will provision it
        themselves), so no APIs/bucket/build are touched — just the region/bucket
        it should use. A step that then finds it un-provisioned surfaces deploy.sh's
        own error."""
        return replace(self.normalise(spec), provisioned_at=now_iso())

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
        from docsynth.studio.mixes import get_mix
        out = f"gs://{project.bucket}/catalogues/{args.pack}/{args.version}"
        cat: dict[str, object] = {"out": out, "version": args.version,
                                  "companies": args.companies,
                                  "products_per_company": args.products_per_company}
        # An LLM mix expands to the exact catalogue.providers/fallback/secrets block
        # deploy.sh already runs — the API keys stay in Secret Manager (the block
        # carries only the secret *names*). No mix ⇒ the procedural build, byte-for-
        # byte as before.
        mix = get_mix(args.mix)
        if mix.is_llm:
            cat |= {"providers": mix.providers_block(), "fallback": mix.fallback_block(),
                    "secrets": mix.secrets_map(), "concurrency": args.concurrency}
            if args.budget_usd:
                cat["budget_usd"] = args.budget_usd
        # tasks > 1 makes the build sharded and resumable over company ranges,
        # worked by that many Cloud Run tasks against the Firestore state store the
        # base config already carries (applies to a procedural build too).
        if args.tasks > 1:
            cat["tasks"] = args.tasks
        config = self._base_config(project) | {"catalogue": cat}
        links = (
            Link("catalogue", out),
            Link("console", _bucket_url(project.bucket,
                                        f"catalogues/{args.pack}/{args.version}", project.id)),
        )
        summary = (f"catalogue {args.version} → {out}"
                   + (f" · {mix.name} ≤ ${args.budget_usd:g}" if mix.is_llm else " · procedural"))
        return self._run(config, ["catalogue"], dry_run, capture,
                         summary=summary, links=links)

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
        # How many Cloud Run tasks work the run (via the atomic claim). parallelism
        # 0 ⇒ run all `tasks` at once.
        config["job"] = {**config["job"], "tasks": args.tasks,
                         "parallelism": args.parallelism or args.tasks}
        base = f"gs://{project.bucket}/runs/{args.run_id}"
        links = (
            Link("documents", f"{base}/documents"),
            Link("golden", f"{base}/golden"),
            Link("console", _bucket_url(project.bucket, f"runs/{args.run_id}", project.id)),
            Link("job", f"{_CONSOLE}/run/jobs?project={project.id}&region={project.region}"),
        )
        # Detached: `deploy` (build/update the job — quick) then `dispatch`, which
        # executes without --wait. A GCP run can take hours and outlive the
        # operator's terminal, so the studio returns handles + links immediately and
        # the run is followed with `docsynth studio status --run …` (decision 1). The
        # blocking `run` subcommand stays available for a hand-run.
        return self._run(config, ["deploy", "dispatch"], dry_run, capture, run_id=args.run_id,
                         summary=f"dispatched {args.total} {args.pack} → {base}", links=links)

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

    def teardown(self, project: Project, *, keep_data: bool = True,
                 dry_run: bool = False, capture: bool = False) -> Result:
        """Delete the project's Cloud Run job(s), and — unless keep_data — the whole
        bucket. Runs deploy.sh's teardown non-interactively (the studio does its own
        confirmation); Firestore and the service account are left in place."""
        config = self._base_config(project)
        env_extra = {"ASSUME_YES": "1"}
        if not keep_data:
            env_extra["TEARDOWN_BUCKET"] = "1"
        scope = "job(s)" if keep_data else "job(s) + bucket (all documents + golden)"
        return self._run(config, ["teardown"], dry_run, capture, env_extra=env_extra,
                         summary=f"teardown {project.ref} — {scope}")

    # ── catalogue re-use ──────────────────────────────────────────────────────
    def catalogue_location(self, project: Project, pack: str, version: str) -> str:
        return f"gs://{project.bucket}/catalogues/{pack}/{version}"

    def catalogue_info(self, project: Project, pack: str, version: str) -> dict | None:
        """The catalogue's manifest if one is already built for this version, else
        None. Read straight from GCS with `gcloud storage cat` (no local gcp extra
        needed); a missing object or absent gcloud is simply 'no catalogue'."""
        import json
        uri = f"{self.catalogue_location(project, pack, version)}/manifest.json"
        try:
            proc = subprocess.run(["gcloud", "storage", "cat", uri, "--project", project.id],
                                  capture_output=True, text=True, check=False)
        except (FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return None

    # ── run.yaml scaffolding ──────────────────────────────────────────────────
    def scaffold(self, step: str, project: Project, args: object, path: str) -> Result:
        """Write the deploy.sh config for a step to ``path`` as a reusable run.yaml
        (instead of a temp file), so the operator can drive `deploy.sh -c <path>`
        directly. Reuses the exact config the run_* methods build."""
        runners = {"catalog": self.run_catalogue, "pdfs": self.run_generate,
                   "export": self.run_export}
        self._config_out = Path(path)
        try:
            return runners[str(step)](project, args, dry_run=True)   # type: ignore[operator]
        finally:
            self._config_out = None

    # ── internals ───────────────────────────────────────────────────────────
    def _base_config(self, project: Project) -> dict:
        return {
            "project": project.id,
            "region": project.region,
            "bucket": project.bucket,
            "firestore": {"database": "(default)", "location": "nam5"},
            "artifact_registry": {"repo": "docsynth", "image_tag": "v1"},
            "job": {"name": "docsynth-generate", "service_account": "docsynth-run",
                    "tasks": 4, "parallelism": 4, "cpu": 2, "memory": "4Gi"},
        }

    def _write_config(self, config: dict) -> Path:
        import yaml
        text = yaml.safe_dump(config, sort_keys=False)
        # `scaffold` sets _config_out to write a named, commented run.yaml instead
        # of a throwaway temp file.
        out = getattr(self, "_config_out", None)
        if out is not None:
            out = Path(out)
            out.write_text("# docsynth run.yaml — scaffolded by `docsynth studio … --scaffold`.\n"
                           "# Drive it directly:  deploy/gcp/deploy.sh -c <this file> <command>\n\n"
                           + text)
            return out
        fd, path = tempfile.mkstemp(suffix=".studio.yaml")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return Path(path)

    def _display(self, cfg: Path, subcommands: list[str]) -> str:
        script = "deploy/gcp/deploy.sh"
        return "  &&  ".join(f"PYTHON={sys.executable} ./{script} -c {cfg} {sub}"
                             for sub in subcommands)

    def _run(self, config: dict, subcommands: list[str], dry_run: bool, capture: bool, *,
             summary: str, links: tuple[Link, ...] = (), run_id: str = "",
             env_extra: dict[str, str] | None = None) -> Result:
        cfg = self._write_config(config)
        command = self._display(cfg, subcommands)
        if dry_run:
            return Result(ok=True, summary=summary, command=command, links=links, run_id=run_id)
        ok, detail = self._sh(cfg, subcommands, capture=capture, env_extra=env_extra)
        return Result(
            ok=ok, summary=summary if ok else f"failed: {summary}", command=command,
            links=links if ok else (), run_id=run_id, detail=detail,
        )

    def _sh(self, cfg: Path, subcommands: list[str], *, capture: bool,
            env_extra: dict[str, str] | None = None) -> tuple[bool, str]:
        """Run each ``deploy.sh -c <cfg> <sub>`` in order; stop at the first failure."""
        script = _deploy_script()
        env = {**os.environ, "PYTHON": sys.executable, **(env_extra or {})}
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
