"""Deployment-target lookup — the studio's equivalent of the pack registry.

Phase 1 ships ``local``. ``gcp`` (Cloud Run Jobs via deploy.sh) lands in phase 3,
``aws``/``azure`` later; naming them here lets the error message say "not yet"
rather than "unknown".
"""
from __future__ import annotations

from docloom.studio.local import LocalTarget
from docloom.studio.types import DeploymentTarget, StudioError

_TARGETS: dict[str, type] = {
    "local": LocalTarget,
}

#: Targets named but not yet built — for a helpful "coming later" message.
_PLANNED = {
    "gcp": "phase 3 (Cloud Run Jobs)",
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
