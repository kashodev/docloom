"""Build the Jinja render context from a :class:`GoldenInvoice`.

Everything the templates need is resolved here — column headers, CSS modifier
classes, grouped line items, jurisdiction-specific totals labels — so the
templates stay declarative. No template performs arithmetic: the golden record
is already the ground truth, and a template that recomputed a total could
silently disagree with it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from docloom.core.money import money
from docloom.packs.invoice.fonts import font_face_css, font_stack
from docloom.packs.invoice.jurisdictions import profile_for
from docloom.packs.invoice.labels import LABEL_REGISTRY
from docloom.packs.invoice.record import GoldenInvoice, LineItem


def column_headers(invoice: GoldenInvoice) -> dict[str, str]:
    """Resolve the company's column vocabulary to printed header text.

    Falls back to the language's ``standard`` vocabulary if the profile names
    one that language does not define — an English archetype rendered in French
    should still produce sensible headers rather than raising.
    """
    return dict(
        LABEL_REGISTRY.vocabulary(invoice.language, invoice.render_profile.column_vocabulary)
    )


def body_classes(invoice: GoldenInvoice) -> str:
    """CSS modifier classes that drive the variation matrix.

    A single archetype plus these classes covers the meta-position, totals, and
    table-styling axes, which is why 39 source PDFs collapse to far fewer
    templates without losing visual diversity.
    """
    p = invoice.render_profile
    classes = [
        f"meta-{p.meta_position}",
        f"totals-{p.totals_style}",
        f"table-{p.table_style}",
        f"lang-{invoice.language.value.lower().replace('-', '_')}",
        "has-logo" if p.has_logo else "no-logo",
    ]
    # The top-row meta position flows its labels horizontally — the banner look.
    if p.meta_position == "top-row":
        classes.append("meta-banner")
    return " ".join(classes)


def group_line_items(items: tuple[LineItem, ...]) -> list[dict[str, Any]]:
    """Group lines by ``group_key`` then ``section``, preserving order.

    Used by hierarchical archetypes (telecom being the extreme case: account →
    subscriber → category → event). Lines with no ``group_key`` land in a
    single unnamed group, so flat invoices use the same structure and the
    templates need only one code path.

    Subtotals are summed from already-rounded line amounts, matching how the
    document prints them.
    """
    groups: list[dict[str, Any]] = []
    index: dict[str | None, dict[str, Any]] = {}

    for item in items:
        group = index.get(item.group_key)
        if group is None:
            group = {
                "key": item.group_key,
                "label": item.group_label,
                "sections": [],
                "_sections": {},
                "subtotal": Decimal("0.00"),
            }
            index[item.group_key] = group
            groups.append(group)

        section = group["_sections"].get(item.section)
        if section is None:
            section = {"name": item.section, "lines": [], "subtotal": Decimal("0.00")}
            group["_sections"][item.section] = section
            group["sections"].append(section)

        section["lines"].append(item)
        section["subtotal"] = money(section["subtotal"] + item.extended_amount)
        group["subtotal"] = money(group["subtotal"] + item.extended_amount)

    for group in groups:
        del group["_sections"]
    return groups


def build_context(invoice: GoldenInvoice) -> dict[str, Any]:
    """Assemble the full render context."""
    jp = profile_for(invoice.jurisdiction)

    # France prints Total HT / TVA / Total TTC rather than subtotal + taxes.
    # The values are identical; only the labelling and ordering differ.
    totals_labels = {
        "subtotal": "subtotal",
        "total": "total" if jp.uses_ht_ttc_totals else "balance_due",
    }

    return {
        # Locale — read by the context-aware formatting filters.
        "locale": invoice.locale,
        "language": invoice.language,
        "labels": LABEL_REGISTRY,
        "currency": invoice.currency,
        # Core record
        "invoice": invoice,
        "issuer": invoice.issuer,
        "recipient": invoice.recipient,
        "totals": invoice.totals,
        "tax_buckets": invoice.tax_buckets,
        "lines": invoice.line_items,
        "groups": group_line_items(invoice.line_items),
        # Presentation
        "profile": invoice.render_profile.model_dump(),
        "body_classes": body_classes(invoice),
        "font_stack": font_stack(invoice.render_profile.typeface),
        "font_face_css": font_face_css(invoice.render_profile.typeface),
        "cols": column_headers(invoice),
        "totals_labels": totals_labels,
        # Jurisdiction behaviour
        "uses_ht_ttc": jp.uses_ht_ttc_totals,
        "requires_late_penalty": jp.requires_late_penalty_notice,
        "registrations": invoice.issuer.registrations,
        # Column presence — templates branch on these rather than sniffing data
        "show_unit_price": "unit_price" in column_headers(invoice),
        "show_quantity": "quantity" in column_headers(invoice),
        # Only vocabularies that actually define the column get one. When the
        # data carries a part number but the vocabulary has no column for it,
        # the renderer prefixes it to the description — which is what ERP
        # systems do, and keeps the identifier visible to an extractor.
        "show_part_number": "part_number" in column_headers(invoice),
        "show_reference": "reference" in column_headers(invoice),
        "has_discount": invoice.totals.discount_total != Decimal("0.00"),
        "has_shipping": invoice.totals.shipping_total != Decimal("0.00"),
        "has_deposit": invoice.totals.deposit != Decimal("0.00"),
        "has_periods": any(li.period_start for li in invoice.line_items),
    }
