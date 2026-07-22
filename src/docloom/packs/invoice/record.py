"""The golden record.

This module is the contract between ``docloom.generate`` (writer) and
``docloom.export`` (reader). It is imported by both so the two cannot drift.

The golden record is *computed*, never extracted: it is the input to rendering,
and the PDF is a projection of it. Any arithmetic here is the ground truth the
extraction pipeline is scored against.

**Golden rows correspond 1:1 with printed rows.** A tiered usage line prints a
parent row plus one sub-row per band, so :meth:`GoldenInvoice.to_line_item_rows`
emits exactly that: the parent (``tier_index`` null, ``tier_count`` set) and one
row per band (``parent_line_no`` set). Anything else would make per-row recall
and precision incomparable between golden and extracted output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from docloom.core.enums import DocumentCondition, Jurisdiction
from docloom.core.locale.enums import Currency, Language, Locale
from docloom.core.record import TableRows
from docloom.packs.invoice.enums import (
    GOODS_KINDS,
    BillingModel,
    BusinessType,
    CodeSystem,
    DiscountScheme,
    DiscountTiming,
    LineItemKind,
    UsageUnit,
)
from docloom.core.money import ZERO, money, sum_money

#: At or below this ``wear`` a document counts as *crisp* — well preserved, not
#: an old or heavily copied artefact. Recorded on the golden row so an evaluation
#: can split accuracy by artefact quality without re-deriving the threshold.
_CRISP_WEAR = 0.25


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaxRegistration(_Base):
    """An issuer tax-registration identifier printed on the document.

    Some are legally mandatory: France requires SIRET and TVA
    intracommunautaire; Quebec requires TPS and TVQ registration numbers.
    Values are format-valid but fictional.
    """

    kind: str = Field(description="SIRET | TVA_INTRA | RCS | APE | GST | QST | VAT | EIN")
    value: str


class Party(_Base):
    """Issuer or recipient."""

    party_id: str
    name: str
    address_lines: tuple[str, ...] = ()
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    registrations: tuple[TaxRegistration, ...] = ()


class TaxBucket(_Base):
    """One tax rate applied to one taxable base.

    Multiple buckets model GST+PST, GST+QST, and multi-rate VAT/TVA invoices
    where different line items attract different rates.
    """

    code: str = Field(description="Stable code, e.g. GST, QST, HST, PST, VAT, TVA, SALES_TAX")
    label: str = Field(description="As printed, localised, e.g. 'TVQ (9,975 %)'")
    rate_percent: Decimal
    taxable_base: Decimal
    amount: Decimal


class PricingTier(_Base):
    """One band of a tiered price, as printed.

    ``upper_bound`` is ``None`` for the unbounded top band ("100,001+"). Under
    :attr:`BillingModel.GRADUATED_TIER` only ``units_in_tier`` are charged at
    ``rate``; under :attr:`BillingModel.VOLUME_TIER` the reached band's rate is
    applied to the whole quantity, so only one tier carries a non-zero amount.
    """

    tier_index: int
    lower_bound: Decimal
    upper_bound: Decimal | None = None
    rate: Decimal
    units_in_tier: Decimal
    amount: Decimal


class LineItem(_Base):
    """A single logical line on the invoice.

    Most fields are optional because one shape has to serve a retail SKU row, a
    repair-labour row, a SaaS subscription with a billing period, a graduated
    usage line with tier bands, and a telecom call-detail event. They become
    nullable columns in the flat ``line_items`` table, so one query shape covers
    every business type.
    """

    line_no: int
    description: str

    kind: LineItemKind = LineItemKind.PRODUCT
    billing_model: BillingModel = BillingModel.PER_UNIT

    # ── Identifiers ────────────────────────────────────────────────────────
    sku: str | None = None
    part_number: str | None = None
    code: str | None = Field(default=None, description="Domain code, e.g. CPT 99213")
    code_system: CodeSystem = CodeSystem.NONE

    # ── Price ──────────────────────────────────────────────────────────────
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = ZERO
    extended_amount: Decimal = ZERO

    discount_percent: Decimal | None = None
    discount_amount: Decimal = ZERO
    tax_code: str | None = Field(default=None, description="Which TaxBucket.code applies")

    # ── Tiered pricing ─────────────────────────────────────────────────────
    tiers: tuple[PricingTier, ...] = ()

    # ── Billing period (subscriptions, metered cycles, statements) ─────────
    period_start: date | None = None
    period_end: date | None = None
    is_prorated: bool = False
    proration_factor: Decimal | None = Field(
        default=None, description="Fraction of the period charged, e.g. 0.4667"
    )

    # ── Allowance vs overage (seat plans, usage bundles) ───────────────────
    included_quantity: Decimal | None = None
    overage_quantity: Decimal | None = None

    # ── Metered usage ──────────────────────────────────────────────────────
    usage_unit: UsageUnit = UsageUnit.NONE
    usage_start: str | None = Field(default=None, description="ISO 8601 timestamp of the event")
    duration_seconds: int | None = None
    volume: Decimal | None = Field(default=None, description="Value in ``usage_unit``")
    usage_type: str | None = Field(default=None, description="e.g. Brwsr, Voice, SMS, Roam")
    counterparty: str | None = Field(default=None, description="Called/called-by number")
    rate_per_unit: Decimal | None = None

    # ── Grouping (subscriber -> category -> rows) ──────────────────────────
    group_key: str | None = None
    group_label: str | None = None
    section: str | None = Field(default=None, description="e.g. 'Packet data', 'Usage'")

    @property
    def is_credit(self) -> bool:
        return self.extended_amount < ZERO

    @property
    def tier_count(self) -> int:
        return len(self.tiers)

    @model_validator(mode="after")
    def _check_tiers_sum(self) -> LineItem:
        """Tier band amounts must sum to the line's extended amount.

        Applies to both tiering models: under VOLUME_TIER the unreached bands
        simply carry a zero amount.
        """
        if not self.tiers:
            return self
        computed = sum_money([t.amount for t in self.tiers])
        if computed != self.extended_amount:
            raise ValueError(
                f"line {self.line_no}: tiers sum to {computed} "
                f"but extended_amount is {self.extended_amount}"
            )
        return self

    @model_validator(mode="after")
    def _check_period(self) -> LineItem:
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError(f"line {self.line_no}: period_end precedes period_start")
        return self


class InvoiceTotals(_Base):
    """The totals block, in the order it is printed.

    ``deposit`` models the "Less Deposit" prepayment seen across the source
    corpus. It is deliberately distinct from ``discount_total``: a deposit is
    money already paid, a discount is a price reduction. Conflating them would
    teach the extractor a relationship that does not exist.
    """

    subtotal: Decimal
    discount_total: Decimal = ZERO
    shipping_total: Decimal = ZERO
    taxable_base: Decimal = ZERO
    tax_total: Decimal = ZERO
    deposit: Decimal = ZERO
    grand_total: Decimal

    @model_validator(mode="after")
    def _check_balances(self) -> InvoiceTotals:
        """Fail loudly if the totals do not reconcile.

        This runs on every generated invoice. A golden record that does not
        balance is worse than no golden record — it would score a correct
        extraction as wrong.
        """
        expected = money(
            self.subtotal
            - self.discount_total
            + self.shipping_total
            + self.tax_total
            - self.deposit
        )
        if expected != self.grand_total:
            raise ValueError(
                f"totals do not reconcile: subtotal {self.subtotal} "
                f"- discount {self.discount_total} + shipping {self.shipping_total} "
                f"+ tax {self.tax_total} - deposit {self.deposit} "
                f"= {expected}, but grand_total is {self.grand_total}"
            )
        return self


class RenderProfile(_Base):
    """The visual identity applied to this invoice.

    Fixed per company (a vendor's invoices must look like the same vendor), so
    these values are drawn once per company and reused. Recorded on the golden
    record so evaluation can slice accuracy by visual treatment — e.g. does the
    extractor do worse on borderless tables or low-contrast accent colours?
    """

    archetype: str = Field(description="HTML archetype slug, e.g. meta-sidebar-01")
    meta_position: str
    totals_style: str
    table_style: str
    column_vocabulary: str
    typeface: str
    accent_color: str
    logo_lockup: str
    has_logo: bool = True
    logo_style: str = "wordmark"   # "wordmark" (text only) or "mark" (procedural SVG + wordmark)
    has_watermark: bool = False    # a faint full-page brand mark behind the content
    font_scale: float = 1.0   # per-company body-text scale (~0.92–1.12), for size variety


class GoldenInvoice(_Base):
    """One synthetic invoice and its complete ground truth."""

    # ── Identity ───────────────────────────────────────────────────────────
    invoice_id: str
    run_id: str
    invoice_index: int = Field(description="Position in the run; with run_id, seeds the PRNG")
    seed: int

    # ── Document header ────────────────────────────────────────────────────
    invoice_number: str
    issue_date: date
    due_date: date | None = None
    purchase_order: str | None = None
    customer_reference: str | None = None

    # Statement / billing cycle, for subscription and metered issuers.
    billing_period_start: date | None = None
    billing_period_end: date | None = None

    # ── Parties ────────────────────────────────────────────────────────────
    issuer: Party
    recipient: Party
    business_type: BusinessType

    # ── Localisation ───────────────────────────────────────────────────────
    # ``locale`` fixes number/date shape; the label vocabulary is derived from
    # it, so en-US and en-GB share wording but not formatting.
    locale: Locale
    jurisdiction: Jurisdiction
    currency: Currency

    # ── Content ────────────────────────────────────────────────────────────
    line_items: tuple[LineItem, ...]
    tax_buckets: tuple[TaxBucket, ...] = ()
    totals: InvoiceTotals

    discount_scheme: DiscountScheme = DiscountScheme.NONE
    discount_timing: DiscountTiming = DiscountTiming.PRE_TAX

    notes: str | None = None
    payment_terms: str | None = None

    # ── Rendering ──────────────────────────────────────────────────────────
    company_id: str
    render_profile: RenderProfile
    condition: DocumentCondition = DocumentCondition.CLEAN
    wear: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "How worn the physical artefact is. 1.0 is the default well-used "
            "look; 0.0 is a crisp document — fresh off the pad, sharply copied. "
            "Drives the ink roughening at render time and the scan degradation "
            "afterwards, so the two stay consistent. Never reaches perfectly "
            "clean lines: real ink and real scanners are not vector-exact."
        ),
    )
    goods_receipt: bool = Field(
        default=False,
        description=(
            "Render a receiver's signature block — the line a customer signs on "
            "taking delivery. Only valid for physical goods: nobody signs a "
            "delivery note for a month of consulting, so this is validated, not "
            "merely documented."
        ),
    )
    received_date: date | None = Field(
        default=None,
        description=(
            "When the customer signed for the goods. Printed on a goods receipt, "
            "so it is an extraction target and belongs in the golden data."
        ),
    )
    page_count: int | None = Field(default=None, description="Filled in after rendering")

    # ── Storage pointers ───────────────────────────────────────────────────
    gcs_pdf_path: str | None = None
    gcs_golden_shard: str | None = None

    @property
    def record_id(self) -> str:
        """Kernel-facing identifier (:class:`~docloom.core.record.GoldenRecord`).

        The field stays ``invoice_id`` because that is what appears in the
        exported table and in evaluation SQL, where a domain-meaningful name is
        worth more than uniformity. The kernel — storage paths, sink keys,
        dedup — only ever asks for ``record_id``.
        """
        return self.invoice_id

    @property
    def is_crisp(self) -> bool:
        """Well preserved rather than old or heavily copied. Exported on the
        golden row so an evaluation can split accuracy by artefact quality."""
        return self.wear <= _CRISP_WEAR

    @property
    def language(self) -> Language:
        """Label vocabulary, derived from the locale."""
        return self.locale.language

    @property
    def line_item_count(self) -> int:
        """Logical lines. Tier sub-rows are not counted here; see
        ``printed_row_count`` for what actually appears on the page."""
        return len(self.line_items)

    @property
    def printed_row_count(self) -> int:
        """Rows an extractor will see, including tier bands."""
        return sum(1 + li.tier_count for li in self.line_items)

    @property
    def billing_models(self) -> tuple[str, ...]:
        """Distinct billing models present, for eval slicing."""
        return tuple(sorted({str(li.billing_model) for li in self.line_items}))

    @model_validator(mode="after")
    def _check_line_items_sum(self) -> GoldenInvoice:
        """The printed line amounts must sum to the printed subtotal.

        Only parent lines are summed — tier bands are components of their
        parent's amount, not independent lines.
        """
        computed = sum_money([li.extended_amount for li in self.line_items])
        if computed != self.totals.subtotal:
            raise ValueError(
                f"line items sum to {computed} but subtotal is {self.totals.subtotal}"
            )
        return self

    @model_validator(mode="after")
    def _check_tax_buckets_sum(self) -> GoldenInvoice:
        """Tax bucket amounts must sum to the printed tax total."""
        if not self.tax_buckets:
            return self
        computed = sum_money([b.amount for b in self.tax_buckets])
        if computed != self.totals.tax_total:
            raise ValueError(
                f"tax buckets sum to {computed} but tax_total is {self.totals.tax_total}"
            )
        return self

    @model_validator(mode="after")
    def _check_goods_receipt_is_physical_goods(self) -> GoldenInvoice:
        """A goods receipt must actually be for goods.

        The receiver's signature line means "I took delivery of these items", so
        it only makes sense when every line is something deliverable. Enforced
        rather than documented: an invoice for consulting hours carrying a
        delivery signature would be a *wrong* training example, and the corpus is
        only worth anything if its artefacts are internally consistent.
        """
        if not self.goods_receipt:
            return self
        if not self.line_items:
            raise ValueError("a goods receipt needs at least one line item")
        if self.received_date is not None and self.received_date < self.issue_date:
            raise ValueError(
                f"received_date {self.received_date} precedes issue_date {self.issue_date}"
            )
        non_goods = sorted({str(li.kind) for li in self.line_items if li.kind not in GOODS_KINDS})
        if non_goods:
            raise ValueError(
                "goods_receipt requires every line to be physical goods "
                f"({sorted(str(k) for k in GOODS_KINDS)}); found {non_goods}"
            )
        return self

    # ── Flattening for the Parquet / BigQuery golden tables ────────────────

    #: Tables this record contributes to, declared for sinks that create or
    #: validate destinations before a run starts.
    TABLES: ClassVar[tuple[str, ...]] = ("invoices", "line_items")

    def to_rows(self) -> TableRows:
        """Flatten to the pack's named tables.

        The kernel calls only this; ``_invoice_row`` and ``_line_item_rows``
        stay private so the table layout is a pack concern.
        """
        return {
            "invoices": [self._invoice_row()],
            "line_items": self._line_item_rows(),
        }

    def _invoice_row(self) -> dict[str, Any]:
        """One row for the ``invoices`` table."""
        return {
            "invoice_id": self.invoice_id,
            "run_id": self.run_id,
            "invoice_index": self.invoice_index,
            "company_id": self.company_id,
            "business_type": str(self.business_type),
            "template_id": self.render_profile.archetype,
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date,
            "due_date": self.due_date,
            "billing_period_start": self.billing_period_start,
            "billing_period_end": self.billing_period_end,
            "locale": str(self.locale),
            "language": str(self.language),
            "jurisdiction": str(self.jurisdiction),
            "currency": str(self.currency),
            "issuer_name": self.issuer.name,
            "recipient_name": self.recipient.name,
            "subtotal": self.totals.subtotal,
            "discount_total": self.totals.discount_total,
            "shipping_total": self.totals.shipping_total,
            "taxable_base": self.totals.taxable_base,
            "tax_total": self.totals.tax_total,
            "deposit": self.totals.deposit,
            "grand_total": self.totals.grand_total,
            "discount_scheme": str(self.discount_scheme),
            "discount_timing": str(self.discount_timing),
            "tax_bucket_count": len(self.tax_buckets),
            "line_item_count": self.line_item_count,
            "printed_row_count": self.printed_row_count,
            "billing_models": list(self.billing_models),
            "condition": str(self.condition),
            "is_handwritten": self.condition == DocumentCondition.HANDWRITTEN,
            "is_degraded": self.condition != DocumentCondition.CLEAN,
            # Recorded so an evaluation can slice accuracy by artefact quality —
            # "how much worse is OCR on a worn copy?" is a question the corpus
            # should be able to answer directly.
            "wear": self.wear,
            "is_crisp": self.is_crisp,
            "goods_receipt": self.goods_receipt,
            "received_date": self.received_date,
            "page_count": self.page_count,
            "meta_position": self.render_profile.meta_position,
            "totals_style": self.render_profile.totals_style,
            "table_style": self.render_profile.table_style,
            "column_vocabulary": self.render_profile.column_vocabulary,
            "typeface": self.render_profile.typeface,
            "font_scale": self.render_profile.font_scale,
            "has_logo": self.render_profile.has_logo,
            "logo_style": self.render_profile.logo_style,
            "has_watermark": self.render_profile.has_watermark,
            "gcs_pdf_path": self.gcs_pdf_path,
            "gcs_golden_shard": self.gcs_golden_shard,
        }

    def _line_item_rows(self) -> list[dict[str, Any]]:
        """One row per *printed* row for the ``line_items`` table.

        A tiered line contributes its parent row plus one row per band, so the
        golden table and an extractor's output can be compared row for row.
        """
        rows: list[dict[str, Any]] = []
        for li in self.line_items:
            rows.append(self._line_row(li))
            rows.extend(self._tier_row(li, t) for t in li.tiers)
        return rows

    def _line_row(self, li: LineItem) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "run_id": self.run_id,
            "line_no": li.line_no,
            "parent_line_no": None,
            "tier_index": None,
            "tier_count": li.tier_count,
            "kind": str(li.kind),
            "billing_model": str(li.billing_model),
            "description": li.description,
            "sku": li.sku,
            "part_number": li.part_number,
            "code": li.code,
            "code_system": str(li.code_system),
            "quantity": li.quantity,
            "unit_price": li.unit_price,
            "discount_percent": li.discount_percent,
            "discount_amount": li.discount_amount,
            "extended_amount": li.extended_amount,
            "tax_code": li.tax_code,
            "is_credit": li.is_credit,
            "tier_lower": None,
            "tier_upper": None,
            "tier_rate": None,
            "period_start": li.period_start,
            "period_end": li.period_end,
            "is_prorated": li.is_prorated,
            "proration_factor": li.proration_factor,
            "included_quantity": li.included_quantity,
            "overage_quantity": li.overage_quantity,
            "usage_unit": str(li.usage_unit),
            "usage_start": li.usage_start,
            "duration_seconds": li.duration_seconds,
            "volume": li.volume,
            "usage_type": li.usage_type,
            "counterparty": li.counterparty,
            "rate_per_unit": li.rate_per_unit,
            "group_key": li.group_key,
            "group_label": li.group_label,
            "section": li.section,
        }

    def _tier_row(self, li: LineItem, tier: PricingTier) -> dict[str, Any]:
        row = self._line_row(li)
        row.update(
            {
                "line_no": li.line_no,
                "parent_line_no": li.line_no,
                "tier_index": tier.tier_index,
                "tier_count": li.tier_count,
                "description": _tier_description(tier, li.usage_unit),
                "quantity": tier.units_in_tier,
                "unit_price": tier.rate,
                "extended_amount": tier.amount,
                "rate_per_unit": tier.rate,
                "tier_lower": tier.lower_bound,
                "tier_upper": tier.upper_bound,
                "tier_rate": tier.rate,
                "discount_percent": None,
                "discount_amount": ZERO,
                "sku": None,
                "part_number": None,
                "code": None,
                "is_credit": tier.amount < ZERO,
            }
        )
        return row


def _tier_description(tier: PricingTier, unit: UsageUnit) -> str:
    """Render a band label the way it is printed, e.g. ``10,001 – 100,000``."""
    lower = f"{tier.lower_bound:,.0f}"
    upper = f"{tier.upper_bound:,.0f}" if tier.upper_bound is not None else "+"
    span = f"{lower} – {upper}" if tier.upper_bound is not None else f"{lower}+"
    return span if unit is UsageUnit.NONE else f"{span} {unit}"
