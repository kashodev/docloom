"""Golden-record schema tests.

These exercise the billing models the dataset has to represent, and — just as
importantly — prove the reconciliation validators reject records that do not
balance. A golden record that silently fails to balance would score a correct
extraction as wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from docloom.core import (
    Currency,
    Jurisdiction,
    Locale,
    money,
    pct,
    sum_money,
)
from docloom.packs.invoice import (
    BillingModel,
    BusinessType,
    CodeSystem,
    DiscountScheme,
    GoldenInvoice,
    InvoiceTotals,
    LineItem,
    LineItemKind,
    Party,
    PricingTier,
    RenderProfile,
    TaxBucket,
    UsageUnit,
)

PROFILE = RenderProfile(
    archetype="meta-sidebar-01",
    meta_position="left-rail",
    totals_style="right-stack",
    table_style="rules-only",
    column_vocabulary="standard",
    typeface="Inter",
    accent_color="#2D6A4F",
    logo_lockup="left",
)


def build(items: list[LineItem], business: BusinessType, **kw: object) -> GoldenInvoice:
    """Assemble a tax-free invoice around the given lines."""
    subtotal = sum_money([i.extended_amount for i in items])
    return GoldenInvoice(
        invoice_id="inv_1",
        run_id="run_test",
        invoice_index=1,
        seed=1,
        invoice_number="TEST-0001",
        issue_date=date(2026, 7, 15),
        issuer=Party(party_id="c1", name="Test Issuer"),
        recipient=Party(party_id="r1", name="Test Recipient"),
        business_type=business,
        locale=Locale.EN_US,
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        line_items=tuple(items),
        totals=InvoiceTotals(subtotal=subtotal, grand_total=subtotal),
        company_id="c1",
        render_profile=PROFILE,
        **kw,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tiered pricing
# ─────────────────────────────────────────────────────────────────────────────

def graduated_line() -> LineItem:
    """150,000 requests billed graduated: each band at its own rate."""
    tiers = (
        PricingTier(tier_index=0, lower_bound=D(0), upper_bound=D(10_000),
                    rate=D("0.0020"), units_in_tier=D(10_000), amount=money(D("20.00"))),
        PricingTier(tier_index=1, lower_bound=D(10_001), upper_bound=D(100_000),
                    rate=D("0.0015"), units_in_tier=D(90_000), amount=money(D("135.00"))),
        PricingTier(tier_index=2, lower_bound=D(100_001), upper_bound=None,
                    rate=D("0.0010"), units_in_tier=D(50_000), amount=money(D("50.00"))),
    )
    return LineItem(
        line_no=1,
        description="API requests",
        kind=LineItemKind.USAGE,
        billing_model=BillingModel.GRADUATED_TIER,
        usage_unit=UsageUnit.API_CALLS,
        quantity=D(150_000),
        extended_amount=sum_money([t.amount for t in tiers]),
        tiers=tiers,
    )


def volume_line() -> LineItem:
    """Same 150,000 requests billed volume: whole quantity at the reached rate."""
    tiers = (
        PricingTier(tier_index=0, lower_bound=D(0), upper_bound=D(10_000),
                    rate=D("0.0020"), units_in_tier=D(0), amount=money(D("0.00"))),
        PricingTier(tier_index=1, lower_bound=D(10_001), upper_bound=D(100_000),
                    rate=D("0.0015"), units_in_tier=D(0), amount=money(D("0.00"))),
        PricingTier(tier_index=2, lower_bound=D(100_001), upper_bound=None,
                    rate=D("0.0010"), units_in_tier=D(150_000), amount=money(D("150.00"))),
    )
    return LineItem(
        line_no=1,
        description="API requests",
        kind=LineItemKind.USAGE,
        billing_model=BillingModel.VOLUME_TIER,
        usage_unit=UsageUnit.API_CALLS,
        quantity=D(150_000),
        extended_amount=sum_money([t.amount for t in tiers]),
        tiers=tiers,
    )


def test_graduated_and_volume_tiers_differ() -> None:
    """The two tiering models must produce different totals from identical
    inputs — that difference is what makes them a real extraction test."""
    assert graduated_line().extended_amount == D("205.00")
    assert volume_line().extended_amount == D("150.00")


def test_tier_sum_validator_rejects_mismatch() -> None:
    with pytest.raises(ValidationError, match="tiers sum to"):
        LineItem(
            line_no=1,
            description="API requests",
            extended_amount=D("999.00"),
            tiers=(
                PricingTier(tier_index=0, lower_bound=D(0), upper_bound=None,
                            rate=D("0.001"), units_in_tier=D(1000), amount=D("1.00")),
            ),
        )


def test_tier_bands_flatten_to_printed_rows() -> None:
    """Golden rows must map 1:1 onto printed rows, so a 3-band tier emits a
    parent row plus three sub-rows."""
    inv = build([graduated_line()], BusinessType.AI_PLATFORM)
    rows = inv.to_rows()["line_items"]

    assert inv.line_item_count == 1
    assert inv.printed_row_count == 4
    assert len(rows) == 4

    parent, *bands = rows
    assert parent["tier_index"] is None
    assert parent["parent_line_no"] is None
    assert parent["tier_count"] == 3

    assert [b["tier_index"] for b in bands] == [0, 1, 2]
    assert all(b["parent_line_no"] == 1 for b in bands)
    assert sum_money([b["extended_amount"] for b in bands]) == parent["extended_amount"]
    # The unbounded top band prints as "100,001+", not a range.
    assert bands[2]["description"].startswith("100,001+")


# ─────────────────────────────────────────────────────────────────────────────
# Business-type coverage
# ─────────────────────────────────────────────────────────────────────────────

def test_tier_and_parent_rows_share_a_column_set() -> None:
    """Parquet requires one schema across all rows of a table.

    ``_tier_row`` derives from ``_line_row`` and overrides a subset, so a field
    added to one and not the other would silently produce ragged rows. This is
    the guard: the key sets must be identical.
    """
    inv = build([graduated_line()], BusinessType.AI_PLATFORM)
    rows = inv.to_rows()["line_items"]
    reference = set(rows[0])
    for row in rows[1:]:
        assert set(row) == reference, f"column drift: {reference ^ set(row)}"


def test_b2b_saas_subscription_seats_and_overage() -> None:
    """A single SaaS invoice mixes recurring subscription, seat allowance with
    overage, and a one-time fee — three billing models on one document."""
    items = [
        LineItem(
            line_no=1, description="Growth plan — monthly",
            kind=LineItemKind.SUBSCRIPTION, billing_model=BillingModel.SUBSCRIPTION,
            quantity=D(1), unit_price=D("499.00"), extended_amount=D("499.00"),
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
        ),
        LineItem(
            line_no=2, description="Additional seats",
            kind=LineItemKind.OVERAGE, billing_model=BillingModel.SEAT_BASED,
            usage_unit=UsageUnit.SEATS,
            included_quantity=D(25), overage_quantity=D(7),
            quantity=D(7), unit_price=D("18.00"), extended_amount=D("126.00"),
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
        ),
        LineItem(
            line_no=3, description="Onboarding — mid-cycle activation",
            kind=LineItemKind.FEE, billing_model=BillingModel.FLAT_RATE,
            quantity=D(1), unit_price=D("750.00"), extended_amount=D("350.00"),
            is_prorated=True, proration_factor=D("0.4667"),
        ),
    ]
    inv = build(items, BusinessType.B2B_SAAS,
                billing_period_start=date(2026, 7, 1),
                billing_period_end=date(2026, 7, 31))

    assert inv.totals.grand_total == D("975.00")
    assert inv.billing_models == ("flat_rate", "seat_based", "subscription")
    row = inv.to_rows()["line_items"][1]
    assert row["included_quantity"] == D(25)
    assert row["overage_quantity"] == D(7)


def test_ai_platform_token_metering() -> None:
    """Input / output / cached tokens at different rates — the shape that was
    unrepresentable before the schema extension."""
    items = [
        LineItem(line_no=1, description="Input tokens (millions)",
                 kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                 usage_unit=UsageUnit.TOKENS_INPUT, volume=D(12_400_000),
                 quantity=D("12.4"), unit_price=D("0.14"),
                 extended_amount=money(D("12.4") * D("0.14"))),
        LineItem(line_no=2, description="Output tokens (millions)",
                 kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                 usage_unit=UsageUnit.TOKENS_OUTPUT, volume=D(3_100_000),
                 quantity=D("3.1"), unit_price=D("0.28"),
                 extended_amount=money(D("3.1") * D("0.28"))),
        LineItem(line_no=3, description="Cached input tokens (millions)",
                 kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                 usage_unit=UsageUnit.TOKENS_CACHED, volume=D(48_000_000),
                 quantity=D("48.0"), unit_price=D("0.0028"),
                 extended_amount=money(D("48.0") * D("0.0028"))),
        LineItem(line_no=4, description="Prepaid credit applied",
                 kind=LineItemKind.CREDIT, billing_model=BillingModel.PREPAID_CREDIT,
                 quantity=D(1), unit_price=D("-2.00"), extended_amount=D("-2.00")),
    ]
    inv = build(items, BusinessType.AI_PLATFORM)

    assert inv.line_items[0].extended_amount == D("1.74")   # 12.4 * 0.14 = 1.736 -> 1.74
    assert inv.line_items[2].extended_amount == D("0.13")   # 48.0 * 0.0028 = 0.1344 -> 0.13
    assert inv.line_items[3].is_credit
    assert inv.totals.grand_total == D("0.74")


def test_auto_repair_labour_and_parts() -> None:
    """Repair invoices split hourly labour from parts carrying MPNs."""
    items = [
        LineItem(line_no=1, description="Diagnostic and brake service",
                 kind=LineItemKind.LABOUR, billing_model=BillingModel.HOURLY_LABOUR,
                 usage_unit=UsageUnit.HOURS,
                 quantity=D("2.5"), unit_price=D("145.00"), extended_amount=D("362.50")),
        LineItem(line_no=2, description="Ceramic brake pad set, front",
                 kind=LineItemKind.PART, billing_model=BillingModel.PER_UNIT,
                 part_number="BP-4471-CF", code="BP-4471-CF", code_system=CodeSystem.MPN,
                 quantity=D(2), unit_price=D("89.99"), extended_amount=D("179.98")),
        LineItem(line_no=3, description="Shop supplies & disposal",
                 kind=LineItemKind.SURCHARGE, billing_model=BillingModel.FEE_SCHEDULE,
                 quantity=D(1), unit_price=D("24.00"), extended_amount=D("24.00")),
    ]
    inv = build(items, BusinessType.AUTO_REPAIR)
    assert inv.totals.grand_total == D("566.48")
    assert inv.to_rows()["line_items"][1]["code_system"] == "mpn"


def test_medical_procedure_codes() -> None:
    items = [
        LineItem(line_no=1, description="Office visit, established patient, 20 min",
                 kind=LineItemKind.SERVICE, code="99213", code_system=CodeSystem.CPT,
                 quantity=D(1), unit_price=D("165.00"), extended_amount=D("165.00")),
        LineItem(line_no=2, description="Venipuncture, routine",
                 kind=LineItemKind.SERVICE, code="36415", code_system=CodeSystem.CPT,
                 quantity=D(1), unit_price=D("18.00"), extended_amount=D("18.00")),
        LineItem(line_no=3, description="Insurance contractual adjustment",
                 kind=LineItemKind.ADJUSTMENT, quantity=D(1),
                 unit_price=D("-73.00"), extended_amount=D("-73.00")),
    ]
    inv = build(items, BusinessType.MEDICAL_CLINIC)
    assert inv.totals.grand_total == D("110.00")
    assert inv.to_rows()["line_items"][0]["code"] == "99213"


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────────────────────

def test_totals_validator_rejects_unbalanced() -> None:
    with pytest.raises(ValidationError, match="do not reconcile"):
        InvoiceTotals(subtotal=D("100.00"), tax_total=D("10.00"), grand_total=D("999.00"))


def test_line_items_must_sum_to_subtotal() -> None:
    item = LineItem(line_no=1, description="x", extended_amount=D("10.00"))
    with pytest.raises(ValidationError, match="line items sum to"):
        GoldenInvoice(
            invoice_id="i", run_id="r", invoice_index=1, seed=1,
            invoice_number="N", issue_date=date(2026, 7, 15),
            issuer=Party(party_id="c", name="C"), recipient=Party(party_id="r", name="R"),
            business_type=BusinessType.RETAIL, locale=Locale.EN_US,
            jurisdiction=Jurisdiction.US, currency=Currency.USD,
            line_items=(item,),
            totals=InvoiceTotals(subtotal=D("999.00"), grand_total=D("999.00")),
            company_id="c", render_profile=PROFILE,
        )


def test_period_validator_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="period_end precedes"):
        LineItem(line_no=1, description="x",
                 period_start=date(2026, 8, 1), period_end=date(2026, 7, 1))


def test_quebec_two_bucket_tax_reconciles() -> None:
    """GST 5% + QST 9.975% on the same base, with a pre-tax summary discount."""
    items = [LineItem(line_no=1, description="Livre", quantity=D(12),
                      unit_price=D("24.95"), extended_amount=money(D(12) * D("24.95")))]
    subtotal = sum_money([i.extended_amount for i in items])
    discount = money(subtotal * D(5) / 100)
    base = money(subtotal - discount)

    buckets = (
        TaxBucket(code="GST", label="TPS (5 %)", rate_percent=D("5.000"),
                  taxable_base=base, amount=pct(D("5.000"), base)),
        TaxBucket(code="QST", label="TVQ (9,975 %)", rate_percent=D("9.975"),
                  taxable_base=base, amount=pct(D("9.975"), base)),
    )
    tax_total = sum_money([b.amount for b in buckets])

    inv = GoldenInvoice(
        invoice_id="i", run_id="r", invoice_index=1, seed=1,
        invoice_number="F-1", issue_date=date(2026, 7, 15),
        issuer=Party(party_id="c", name="Éditions Mirada"),
        recipient=Party(party_id="r", name="Librairie Richelieu"),
        business_type=BusinessType.RETAIL, locale=Locale.FR_CA,
        jurisdiction=Jurisdiction.CA_QC, currency=Currency.CAD,
        line_items=tuple(items), tax_buckets=buckets,
        totals=InvoiceTotals(subtotal=subtotal, discount_total=discount,
                             taxable_base=base, tax_total=tax_total,
                             grand_total=money(base + tax_total)),
        discount_scheme=DiscountScheme.SUMMARY_PERCENT,
        company_id="c", render_profile=PROFILE,
    )
    # 299.40 subtotal - 14.97 discount = 284.43 base.
    # GST  284.43 x 5%     = 14.2215     -> 14.22
    # QST  284.43 x 9.975% = 28.3718925  -> 28.37
    # Each bucket is rounded independently, as printed — summing unrounded and
    # rounding once would give 42.60 and disagree with the document by a cent.
    assert inv.tax_buckets[0].amount == D("14.22")
    assert inv.tax_buckets[1].amount == D("28.37")
    assert inv.totals.tax_total == D("42.59")
    assert inv.totals.grand_total == D("327.02")
