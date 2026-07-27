"""Deployment-target lookup — the studio's equivalent of the pack registry.

``local`` (this machine) and ``gcp`` (Cloud Run Jobs via deploy.sh) ship;
``aws``/``azure`` are named so the error message says "not yet" rather than
"unknown".
"""
from __future__ import annotations

from docsynth.studio.gcp import GcpTarget
from docsynth.studio.local import LocalTarget
from docsynth.studio.types import DeploymentTarget, StudioError

_TARGETS: dict[str, type] = {
    "local": LocalTarget,
    "gcp": GcpTarget,
}

#: Targets named but not yet built — for a helpful "coming later" message.
_PLANNED = {
    "aws": "a later phase",
    "azure": "a later phase",
}


def available_targets() -> tuple[str, ...]:
    return tuple(_TARGETS)


def get_target(name: str) -> DeploymentTarget:
    if name in _TARGETS:
        return _TARGETS[name]()
    if name in _PLANNED:
        raise StudioError(
            f"deployment target {name!r} is not available yet — it arrives in "
            f"{_PLANNED[name]}. Available now: {', '.join(_TARGETS)}."
        )
    raise StudioError(
        f"unknown deployment target {name!r}; available: {', '.join(_TARGETS)}"
    )
