"""The studio's memory — a human-readable list of provisioned projects.

One YAML file (``~/.docloom/projects.yaml``, or ``$DOCLOOM_HOME``). It is a
*cache* over what each target already knows, so a lost or hand-edited file is
never fatal — provisioning is idempotent and re-discovers. Two hard rules: it
records secret **names** only (never values — the values live in the cloud's
secret store), and adding a project that already exists **adopts** it rather than
erroring, mirroring idempotent provisioning.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from docloom.studio.types import Project


def home() -> Path:
    """Where the registry lives — ``$DOCLOOM_HOME`` or ``~/.docloom``."""
    return Path(os.environ.get("DOCLOOM_HOME") or (Path.home() / ".docloom"))


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_dict(p: Project) -> dict:
    d: dict[str, object] = {"target": p.target, "id": p.id}
    for key in ("region", "bucket", "root", "provisioned_at", "created_at", "last_run"):
        if value := getattr(p, key):
            d[key] = value
    if p.resources:
        d["resources"] = dict(p.resources)
    if p.secrets_present:
        d["secrets_present"] = list(p.secrets_present)
    return d


def _from_dict(d: dict) -> Project:
    return Project(
        target=str(d["target"]),
        id=str(d["id"]),
        region=d.get("region", ""),
        bucket=d.get("bucket", ""),
        root=d.get("root", ""),
        resources=dict(d.get("resources") or {}),
        secrets_present=tuple(d.get("secrets_present") or ()),
        provisioned_at=d.get("provisioned_at", ""),
        created_at=d.get("created_at", ""),
        last_run=d.get("last_run", ""),
    )


class Registry:
    """Read/write the saved-projects file. Cheap; reads on every call so two
    processes never race on stale in-memory state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (home() / "projects.yaml")

    def _read(self) -> dict:
        import yaml
        if not self.path.is_file():
            return {"version": 1, "default": "", "projects": []}
        data = yaml.safe_load(self.path.read_text()) or {}
        data.setdefault("version", 1)
        data.setdefault("default", "")
        data.setdefault("projects", [])
        return data

    def _write(self, data: dict) -> None:
        import yaml
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False))

    def projects(self) -> list[Project]:
        return [_from_dict(d) for d in self._read()["projects"]]

    def get(self, ref: str) -> Project | None:
        return next((p for p in self.projects() if p.ref == ref), None)

    def default_ref(self) -> str:
        return self._read().get("default", "")

    def add(self, project: Project, *, make_default: bool = False) -> None:
        """Upsert — a project with the same ``ref`` is replaced, not duplicated,
        so re-provisioning is safe. Becomes the default if asked, or if it is the
        first project on file."""
        data = self._read()
        data["projects"] = [
            d for d in data["projects"]
            if not (d["target"] == project.target and str(d["id"]) == project.id)
        ]
        data["projects"].append(_to_dict(project))
        if make_default or not data.get("default"):
            data["default"] = project.ref
        self._write(data)

    def set_last_run(self, ref: str, run_id: str) -> None:
        if (project := self.get(ref)) is not None:
            self.add(replace(project, last_run=run_id))

    def remove(self, ref: str) -> bool:
        """Forget a project by ref. Returns whether it was present. If it was the
        default, the default falls to the first remaining project (or clears)."""
        data = self._read()
        kept = [d for d in data["projects"] if _from_dict(d).ref != ref]
        if len(kept) == len(data["projects"]):
            return False
        data["projects"] = kept
        if data.get("default") == ref:
            data["default"] = _from_dict(kept[0]).ref if kept else ""
        self._write(data)
        return True
