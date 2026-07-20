"""Invoice document pack.

Owns everything invoice-shaped: the golden record, billing vocabularies, tax
profiles, printed labels, archetype templates, and the record→context mapping.
The kernel supplies locale formatting, the Jinja environment, storage, run
state, providers, and sinks — none of which know what an invoice is.

This is the reference implementation of :class:`~docloom.core.pack.DocumentPack`;
a contract or delivery-note pack has the same shape and reuses the same kernel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docloom.core.locale.labels import LabelRegistry
from docloom.core.record import GoldenRecord
from docloom.packs.invoice.context import (
    body_classes,
    build_context,
    column_headers,
    group_line_items,
)
from docloom.packs.invoice.enums import (
    BillingModel,
    BusinessType,
    CodeSystem,
    DiscountScheme,
    DiscountTiming,
    LineItemKind,
    UsageUnit,
)
from docloom.packs.invoice.jurisdictions import (
    PROFILES,
    US_STATE_SALES_TAX,
    JurisdictionProfile,
    TaxRule,
    profile_for,
)
from docloom.packs.invoice.labels import LABEL_REGISTRY
from docloom.packs.invoice.record import (
    GoldenInvoice,
    InvoiceTotals,
    LineItem,
    Party,
    PricingTier,
    RenderProfile,
    TaxBucket,
    TaxRegistration,
)

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"


class InvoicePack:
    """Teaches the kernel how to render and export invoices."""

    name = "invoice"

    @property
    def template_root(self) -> Path:
        return TEMPLATE_ROOT

    @property
    def labels(self) -> LabelRegistry:
        return LABEL_REGISTRY

    @property
    def table_names(self) -> tuple[str, ...]:
        return GoldenInvoice.TABLES

    def build_context(self, record: GoldenRecord) -> dict[str, Any]:
        assert isinstance(record, GoldenInvoice)
        return build_context(record)

    def archetype_for(self, record: GoldenRecord) -> str:
        assert isinstance(record, GoldenInvoice)
        return record.render_profile.archetype


__all__ = [
    "LABEL_REGISTRY",
    "PROFILES",
    "TEMPLATE_ROOT",
    "US_STATE_SALES_TAX",
    "BillingModel",
    "BusinessType",
    "CodeSystem",
    "DiscountScheme",
    "DiscountTiming",
    "GoldenInvoice",
    "InvoicePack",
    "InvoiceTotals",
    "JurisdictionProfile",
    "LineItem",
    "LineItemKind",
    "Party",
    "PricingTier",
    "RenderProfile",
    "TaxBucket",
    "TaxRegistration",
    "TaxRule",
    "UsageUnit",
    "body_classes",
    "build_context",
    "column_headers",
    "group_line_items",
    "profile_for",
]
