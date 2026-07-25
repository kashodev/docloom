"""`docloom studio` — an interactive/scriptable orchestrator over the CLI.

Phase 1 (this module set): the type surface, the project registry, the
``local`` deployment target, and the flag-driven ``studio`` command. It invents
no generation machinery — every step shells into the existing ``docloom`` CLI.
The interactive wizard and cloud targets land in later phases; see
``feature_explorations/interactive-cli-studio.md`` for the full design.
"""
from docloom.studio.gcp import GcpTarget
from docloom.studio.local import LocalTarget
from docloom.studio.registry import Registry
from docloom.studio.targets import available_targets, get_target
from docloom.studio.types import (
    CatalogueArgs,
    DeploymentTarget,
    ExportArgs,
    GenerateArgs,
    Link,
    Project,
    ProjectSpec,
    Result,
    Step,
    StudioError,
)

__all__ = [
    "CatalogueArgs",
    "DeploymentTarget",
    "ExportArgs",
    "GcpTarget",
    "GenerateArgs",
    "Link",
    "LocalTarget",
    "Project",
    "ProjectSpec",
    "Registry",
    "Result",
    "Step",
    "StudioError",
    "available_targets",
    "get_target",
]
