"""Shared builders for tests.

Deliberately explicit rather than randomised: a test that fails should point at
a specific value, not at a seed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

from docsynth.core import (
    Currency,
    Jurisdiction,
    Locale,
    money,
    pct,
    sum_money,
)
from docsynth.packs.invoice import (
    BillingModel,
    BusinessType,
    GoldenInvoice,
    InvoiceTotals,
    LineItem,
    LineItemKind,
    Party,
    PricingTier,
    RenderProfile,
    TaxBucket,
    TaxRegistration,
    UsageUnit,
)


def profile(**kw: Any) -> RenderProfile:
    base: dict[str, Any] = {
        "archetype": "meta-sidebar-01",
        "meta_position": "left-rail",
        "totals_style": "right-stack",
        "table_style": "rules-only",
        "column_vocabulary": "standard",
        "typeface": "Inter",
        "accent_color": "#2D6A4F",
        "logo_lockup": "left",
        "has_logo": True,
    }
    base.update(kw)
    return RenderProfile(**base)


def invoice(
    lines: list[LineItem],
    *,
    locale: Locale = Locale.EN_US,
    jurisdiction: Jurisdiction = Jurisdiction.US,
    currency: Currency = Currency.USD,
    business_type: BusinessType = BusinessType.RETAIL,
    tax_buckets: tuple[TaxBucket, ...] = (),
    registrations: tuple[TaxRegistration, ...] = (),
    **kw: Any,
) -> GoldenInvoice:
    subtotal = sum_money([li.extended_amount for li in lines])
    tax_total = sum_money([b.amount for b in tax_buckets])
    return GoldenInvoice(
        invoice_id="inv_test",
        run_id="run_test",
        invoice_index=1,
        seed=7,
        invoice_number="INV-2026-0042",
        issue_date=date(2026, 7, 15),
        due_date=date(2026, 8, 14),
        issuer=Party(
            party_id="c1",
            name="Northwind Supply",
            address_lines=("100 Fifth Avenue",),
            city="New York",
            region="NY",
            postal_code="10011",
            registrations=registrations,
        ),
        recipient=Party(party_id="r1", name="Acme Industrial"),
        business_type=business_type,
        locale=locale,
        jurisdiction=jurisdiction,
        currency=currency,
        line_items=tuple(lines),
        tax_buckets=tax_buckets,
        totals=InvoiceTotals(
            subtotal=subtotal,
            taxable_base=subtotal if tax_buckets else D("0.00"),
            tax_total=tax_total,
            grand_total=money(subtotal + tax_total),
        ),
        company_id="c1",
        render_profile=kw.pop("render_profile", profile()),
        **kw,
    )


def simple_lines() -> list[LineItem]:
    return [
        LineItem(
            line_no=1,
            description="Stainless steel hex bolt, M8 x 40mm",
            sku="HB-M8-40-SS",
            part_number="HB-M8-40-SS",
            quantity=D(250),
            unit_price=D("0.42"),
            extended_amount=D("105.00"),
        ),
        LineItem(
            line_no=2,
            description="Nitrile gasket set, 3-inch flange",
            sku="GS-300-NBR",
            quantity=D(12),
            unit_price=D("8.75"),
            extended_amount=D("105.00"),
        ),
    ]


def tiered_line() -> LineItem:
    """150,000 API calls billed graduated across three bands."""
    tiers = (
        PricingTier(tier_index=0, lower_bound=D(0), upper_bound=D(10_000),
                    rate=D("0.0020"), units_in_tier=D(10_000), amount=D("20.00")),
        PricingTier(tier_index=1, lower_bound=D(10_001), upper_bound=D(100_000),
                    rate=D("0.0015"), units_in_tier=D(90_000), amount=D("135.00")),
        PricingTier(tier_index=2, lower_bound=D(100_001), upper_bound=None,
                    rate=D("0.0010"), units_in_tier=D(50_000), amount=D("50.00")),
    )
    return LineItem(
        line_no=1,
        description="API requests",
        kind=LineItemKind.USAGE,
        billing_model=BillingModel.GRADUATED_TIER,
        usage_unit=UsageUnit.API_CALLS,
        quantity=D(150_000),
        extended_amount=D("205.00"),
        tiers=tiers,
    )


def telecom_lines() -> list[LineItem]:
    """Two subscribers, each with two categories — the Bell hierarchy in
    miniature."""
    out: list[LineItem] = []
    n = 0
    for msisdn in ("(416) 555-0142", "(416) 555-0188"):
        for section, kind, amount in (
            ("Monthly charges", LineItemKind.SUBSCRIPTION, D("65.00")),
            ("Packet data", LineItemKind.USAGE, D("2.50")),
        ):
            n += 1
            out.append(
                LineItem(
                    line_no=n,
                    description=f"{section} entry",
                    kind=kind,
                    usage_unit=UsageUnit.MEGABYTES if kind is LineItemKind.USAGE
                    else UsageUnit.NONE,
                    volume=D("512.4") if kind is LineItemKind.USAGE else None,
                    usage_type="Brwsr" if kind is LineItemKind.USAGE else None,
                    quantity=D(1),
                    unit_price=amount,
                    extended_amount=amount,
                    group_key=msisdn,
                    group_label=msisdn,
                    section=section,
                )
            )
    return out


def quebec_tax(base: D) -> tuple[TaxBucket, ...]:
    return (
        TaxBucket(code="GST", label="TPS (5 %)", rate_percent=D("5.000"),
                  taxable_base=base, amount=pct(D("5.000"), base)),
        TaxBucket(code="QST", label="TVQ (9,975 %)", rate_percent=D("9.975"),
                  taxable_base=base, amount=pct(D("9.975"), base)),
    )


def france_tax(base: D) -> tuple[TaxBucket, ...]:
    return (
        TaxBucket(code="TVA", label="TVA (20 %)", rate_percent=D("20.000"),
                  taxable_base=base, amount=pct(D("20.000"), base)),
    )
