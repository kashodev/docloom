"""docloom kernel — everything that is not specific to one document type.

Weaves two threads into one artefact: a template (structure) and a golden
record (data). The kernel owns the loom; a :class:`~docloom.core.pack.DocumentPack`
supplies the pattern.
"""

from docloom.core.enums import (
    DocumentCondition,
    Jurisdiction,
    RunState,
    WorkUnitState,
)
from docloom.core.locale import Currency, LabelRegistry, Language, Locale
from docloom.core.money import ZERO, money, pct, sum_money
from docloom.core.pack import DocumentPack
from docloom.core.record import GoldenRecord, TableRows
from docloom.core.registry import available_packs, get_pack, register_pack
from docloom.core.render import build_environment, render_record, render_template

__all__ = [
    "ZERO",
    "Currency",
    "DocumentCondition",
    "DocumentPack",
    "GoldenRecord",
    "Jurisdiction",
    "LabelRegistry",
    "Language",
    "Locale",
    "RunState",
    "TableRows",
    "WorkUnitState",
    "available_packs",
    "build_environment",
    "get_pack",
    "money",
    "pct",
    "register_pack",
    "render_record",
    "render_template",
    "sum_money",
]
