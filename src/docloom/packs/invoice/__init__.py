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

from docloom.core.content import ContentCapability, ContentMode
from docloom.core.enums import DocumentCondition
from docloom.core.locale.labels import LabelRegistry
from docloom.core.pack import RunningHeader
from docloom.core.record import GoldenRecord
from docloom.packs.invoice.catalog import (
    BusinessSpec,
    Catalogue,
    Company,
    CompanyRoster,
    ProductTemplate,
    SeedCatalogue,
)
from docloom.packs.invoice.context import (
    body_classes,
    build_context,
    column_headers,
    group_line_items,
)
from docloom.packs.invoice.sampler import InvoiceSampler
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

#: The archetype a HANDWRITTEN document is always rendered with — a pre-printed
#: pad filled in by hand, rather than any of the typeset layouts.
HANDWRITTEN_ARCHETYPE = "handwritten-form-01"


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
    def content_capability(self) -> ContentCapability:
        """Invoices are procedural — and that is what makes them local-first.

        Descriptions, companies and figures all come from the built-in seed
        catalogue and are computed, so hundreds of thousands of invoices generate
        on a laptop with no key. A contract pack would declare ``LLM_BACKED``.
        """
        return ContentCapability(
            ContentMode.PROCEDURAL,
            notes="Seed catalogue + computed money; deterministic from "
                  "stable_seed(run_id, index). No LLM, no API key.",
        )

    @property
    def table_names(self) -> tuple[str, ...]:
        return GoldenInvoice.TABLES

    def build_context(self, record: GoldenRecord) -> dict[str, Any]:
        assert isinstance(record, GoldenInvoice)
        return build_context(record)

    def archetype_for(self, record: GoldenRecord) -> str:
        """Template for this record — the capture condition can override it.

        A handwritten invoice is a different *document*, not a filtered one: it
        needs the pre-printed pad with ruled lines that someone wrote into, which
        no amount of post-processing can synthesise from a typeset layout. So
        ``HANDWRITTEN`` routes to the hand-filled archetype regardless of the
        company's usual look. The record is untouched — same values, same golden
        rows as the clean twin.
        """
        assert isinstance(record, GoldenInvoice)
        if record.condition is DocumentCondition.HANDWRITTEN:
            return HANDWRITTEN_ARCHETYPE
        return record.render_profile.archetype

    def header_fields(self, record: GoldenRecord) -> RunningHeader:
        """Issuer + invoice number on the left, localised page counter template
        on the right. The renderer composes nothing invoice-specific itself."""
        assert isinstance(record, GoldenInvoice)
        language = record.locale.language
        try:
            number_label = LABEL_REGISTRY.get(language, "invoice_number")
            page_label = LABEL_REGISTRY.get(language, "page_of")
        except KeyError:
            number_label, page_label = "", "Page {page} of {pages}"
        issuer = record.issuer.name or ""
        number = record.invoice_number or ""
        primary = f"{issuer} — {number_label} {number}" if issuer else f"{number_label} {number}"
        return RunningHeader(primary=primary, page_label=page_label)

    def default_source(self) -> InvoiceSampler:
        """The sampler over the procedural seed catalogue — no API keys."""
        return InvoiceSampler()


__all__ = [
    "LABEL_REGISTRY",
    "PROFILES",
    "TEMPLATE_ROOT",
    "US_STATE_SALES_TAX",
    "BillingModel",
    "BusinessSpec",
    "BusinessType",
    "Catalogue",
    "Company",
    "CompanyRoster",
    "InvoiceSampler",
    "ProductTemplate",
    "SeedCatalogue",
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
