"""docsynth kernel — everything that is not specific to one document type.

Weaves two threads into one artefact: a template (structure) and a golden
record (data). The kernel owns the loom; a :class:`~docsynth.core.pack.DocumentPack`
supplies the pattern.
"""

from docsynth.core.content import (
    ContentCapability,
    ContentMode,
    LlmContentBuilder,
    build_catalogue,
    capability_of,
)
from docsynth.core.enums import (
    DocumentCondition,
    Jurisdiction,
    RunState,
    WorkUnitState,
)
from docsynth.core.locale import Currency, LabelRegistry, Language, Locale
from docsynth.core.money import ZERO, money, pct, sum_money
from docsynth.core.pack import DocumentPack
from docsynth.core.record import GoldenRecord, TableRows
from docsynth.core.registry import available_packs, get_pack, register_pack
from docsynth.core.render import build_environment, render_record, render_template

__all__ = [
    "ZERO",
    "ContentCapability",
    "ContentMode",
    "Currency",
    "DocumentCondition",
    "DocumentPack",
    "GoldenRecord",
    "Jurisdiction",
    "LabelRegistry",
    "Language",
    "LlmContentBuilder",
    "Locale",
    "RunState",
    "TableRows",
    "WorkUnitState",
    "available_packs",
    "build_catalogue",
    "build_environment",
    "capability_of",
    "get_pack",
    "money",
    "pct",
    "register_pack",
    "render_record",
    "render_template",
    "sum_money",
]
