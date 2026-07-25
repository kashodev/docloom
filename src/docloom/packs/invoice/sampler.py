"""Invoice sampler — the invoice pack's :class:`DocumentSource`.

Turns a document index into a computed :class:`GoldenInvoice`: draw a company
(weighted, so some issue far more), draw its line items from the catalogue with
billing-model-correct arithmetic, apply discount / shipping / deposit and
jurisdiction tax, and assemble totals that reconcile.

**Everything is a pure function of (run_id, index).** The RNG is seeded from
:func:`stable_seed` — a process-stable hash, not the salted built-in ``hash()``
— so the same index always produces the same invoice, across workers and across
resume. That is what makes retries idempotent and runs reproducible.

The arithmetic is the load-bearing part: the record's validators reject anything
that does not balance, so the pricing helpers below are written to produce
exact, printable amounts (``Decimal``, quantised where the value is printed).
The catalogue supplies content; this module supplies correctness.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from random import Random

from docloom.core.money import ZERO, money, pct, sum_money
from docloom.core.pipeline.source import stable_seed
from docloom.core.selection import Selection
from docloom.packs.invoice.catalog import Catalogue, ProductTemplate, SeedCatalogue
from docloom.packs.invoice.composition import Composition, resolve
from docloom.packs.invoice.enums import (
    EARLIEST_ISSUE_DATE,
    GOODS_KINDS,
    BillingModel,
    BusinessType,
    DiscountScheme,
    DiscountTiming,
    LineItemKind,
    UsageUnit,
)
from docloom.packs.invoice.jurisdictions import profile_for
from docloom.packs.invoice.record import (
    GoldenInvoice,
    InvoiceTotals,
    LineItem,
    Party,
    PricingTier,
    TaxBucket,
)
from docloom.packs.invoice.labels import LABEL_REGISTRY

# Default issue-date window when a slice does not pin one. Kept as the range the
# sampler used before issue dates were configurable (2026-01-01 plus up to 330
# days), so an unconfigured run behaves exactly as it did.
_DEFAULT_ISSUE_START = date(2026, 1, 1)
_DEFAULT_ISSUE_END = _DEFAULT_ISSUE_START + timedelta(days=330)
_RECIPIENTS = ("Acme Industrial", "Blue Harbor Trading", "Nexa Logistics",
               "Pemberton & Rowe", "Crestline Holdings", "Vireo Labs", "Tidewater Group")


def _places_for(high: Decimal) -> int:
    """Sub-dollar rates (AI tokens, telecom) need 4dp; everything else 2dp."""
    return 4 if high < Decimal("1") else 2


def _price(rng: Random, low: Decimal, high: Decimal) -> Decimal:
    span = high - low
    value = low + span * Decimal(str(rng.random()))
    q = Decimal(10) ** -_places_for(high)
    return value.quantize(q, rounding=ROUND_HALF_UP)


def _period(rng: Random, as_of: date) -> tuple[date, date]:
    """A ~monthly billing period the invoice bills in arrears: it ends on or just
    before ``as_of`` (the issue date), so the document is always issued at or after
    the period it covers and every period date stays on or before the issue date —
    logically bound to whatever issue-date window the run asked for."""
    end = as_of - timedelta(days=rng.randint(0, 5))
    return end - timedelta(days=29), end


# ─────────────────────────────────────────────────────────────────────────────
# Per-billing-model line construction — each returns an exact LineItem
# ─────────────────────────────────────────────────────────────────────────────
def _draw_products(
    rng: Random, products: tuple[ProductTemplate, ...], n: int
) -> list[ProductTemplate]:
    """``n`` products, distinct until the pool is exhausted, then reshuffled.

    Drawing with ``rng.choice`` — which is what this replaced — samples *with*
    replacement, so a 6-line invoice against a 5-product pool almost always
    listed the same product twice. Real invoices do not repeat a line; they
    increase its quantity. It was the most visible unrealism in the corpus and
    showed up plainly in the committed samples.

    Cycling through reshuffled permutations rather than sampling without
    replacement outright, because ``n`` legitimately exceeds the pool: a telecom
    invoice bills 60–400 lines from a handful of usage products. Each pass uses
    every product once before any repeats, so repetition is as spread out as the
    pool allows, and reshuffling each pass avoids the tell-tale strict rotation
    (A, B, C, A, B, C…) that a single cycled permutation would produce.
    """
    if not products:
        return []
    drawn: list[ProductTemplate] = []
    while len(drawn) < n:
        pool = list(products)
        rng.shuffle(pool)
        drawn.extend(pool[: n - len(drawn)])
    return drawn


def _build_line(rng: Random, line_no: int, product: ProductTemplate, language: object,
                as_of: date) -> LineItem:
    unit_price = _price(rng, product.price_low, product.price_high)
    code = f"{product.code_prefix}-{rng.randint(1000, 99999)}" if product.code_prefix else None
    common = {
        "line_no": line_no, "description": product.describe(language), "kind": product.kind,
        "billing_model": product.billing_model, "usage_unit": product.usage_unit,
        "code": code, "code_system": product.code_system,
        "sku": code if product.code_system.value == "sku" else None,
        "part_number": code if product.code_system.value == "mpn" else None,
    }
    model = product.billing_model

    if model is BillingModel.FLAT_RATE:
        return LineItem(**common, quantity=Decimal(1), unit_price=unit_price,
                        extended_amount=money(unit_price))

    if model is BillingModel.SUBSCRIPTION:
        start, end = _period(rng, as_of)
        return LineItem(**common, quantity=Decimal(1), unit_price=unit_price,
                        extended_amount=money(unit_price), period_start=start, period_end=end)

    if model is BillingModel.HOURLY_LABOUR:
        hours = Decimal(rng.randint(1, 16)) / 2      # 0.5-hour increments
        return LineItem(**common, quantity=hours, unit_price=unit_price,
                        extended_amount=money(hours * unit_price))

    if model is BillingModel.SEAT_BASED:
        included = Decimal(rng.randint(5, 50))
        overage = Decimal(rng.randint(1, 20))
        return LineItem(**common, quantity=overage, unit_price=unit_price,
                        included_quantity=included, overage_quantity=overage,
                        extended_amount=money(overage * unit_price))

    if model is BillingModel.METERED_USAGE:
        volume = Decimal(rng.randint(1, 5000)) / 10
        return LineItem(**common, quantity=volume, unit_price=unit_price, volume=volume,
                        rate_per_unit=unit_price, extended_amount=money(volume * unit_price))

    if model is BillingModel.GRADUATED_TIER:
        return _graduated_line(rng, common, product)

    # PER_UNIT (default)
    qty = Decimal(rng.randint(1, 500))
    return LineItem(**common, quantity=qty, unit_price=unit_price,
                    extended_amount=money(qty * unit_price))


def _graduated_line(rng: Random, common: dict, product: ProductTemplate) -> LineItem:  # noqa: ARG001
    """Three graduated bands, each charged at its own (decreasing) rate.

    The band amounts sum to ``extended_amount`` — the tier validator enforces it.
    """
    bands: list[tuple[Decimal, Decimal | None]] = [
        (Decimal(0), Decimal(10_000)),
        (Decimal(10_000), Decimal(100_000)),
        (Decimal(100_000), None),
    ]
    rates = sorted(
        (_price(rng, product.price_low, product.price_high) for _ in bands), reverse=True
    )
    total = Decimal(rng.randint(1_000, 250_000))

    tiers: list[PricingTier] = []
    remaining = total
    for i, (lo, hi) in enumerate(bands):
        cap = (hi - lo) if hi is not None else remaining
        units = min(remaining, cap) if remaining > 0 else Decimal(0)
        tiers.append(PricingTier(tier_index=i, lower_bound=lo, upper_bound=hi,
                                 rate=rates[i], units_in_tier=units,
                                 amount=money(units * rates[i])))
        remaining -= units

    extended = sum_money([t.amount for t in tiers])
    return LineItem(**common, quantity=total, extended_amount=extended, tiers=tuple(tiers))


_TELECOM_SECTION = {
    "megabytes": "Data usage", "gigabytes": "Data usage",
    "minutes": "Voice", "seconds": "Voice", "messages": "Messaging",
}
_TELECOM_SECTION_FR = {
    "megabytes": "Données", "gigabytes": "Données",
    "minutes": "Appels", "seconds": "Appels", "messages": "Messagerie",
}


def _telecom_grouping(
    rng: Random, lines: tuple[LineItem, ...], language: object
) -> tuple[LineItem, ...]:
    """Reshape flat telecom lines into a subscriber → category → event hierarchy.

    Amounts are untouched (grouping is presentational), so the totals still
    reconcile; it just gives the telecom archetype the group/section/timestamp
    structure it renders. Without this, generated telecom invoices would render
    as one flat table and the archetype's hierarchy would never be exercised.
    """
    subscribers = [
        f"({rng.randint(200, 989)}) 555-{rng.randint(1000, 9999):04d}"
        for _ in range(rng.randint(1, 3))
    ]
    is_fr = str(language).startswith("fr")
    sections = _TELECOM_SECTION_FR if is_fr else _TELECOM_SECTION
    default_section = "Utilisation" if is_fr else "Usage"
    out = []
    for li in lines:
        sub = rng.choice(subscribers)
        section = sections.get(li.usage_unit.value, default_section)
        stamp = f"{rng.randint(1, 28):02d} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"
        out.append(li.model_copy(update={
            "group_key": sub, "group_label": sub, "section": section, "usage_start": stamp,
        }))
    return tuple(out)


# ─────────────────────────────────────────────────────────────────────────────
# Totals — discount, shipping, deposit, tax
# ─────────────────────────────────────────────────────────────────────────────
def _discount(rng: Random, subtotal: Decimal) -> tuple[DiscountScheme, DiscountTiming, Decimal]:
    if rng.random() >= 0.30:
        return DiscountScheme.NONE, DiscountTiming.PRE_TAX, ZERO
    timing = DiscountTiming.PRE_TAX if rng.random() < 0.7 else DiscountTiming.POST_TAX
    if rng.random() < 0.7:
        rate = Decimal(rng.choice([5, 10, 12, 15, 20]))
        return DiscountScheme.SUMMARY_PERCENT, timing, money(subtotal * rate / 100)
    flat = money(subtotal * Decimal(str(rng.uniform(0.05, 0.25))))
    return DiscountScheme.SUMMARY_AMOUNT, timing, flat


def _tax_buckets(jurisdiction, taxable_base: Decimal, locale) -> tuple[TaxBucket, ...]:  # noqa: ANN001
    """Default-rate buckets for the jurisdiction, each rounded independently."""
    profile = profile_for(jurisdiction)
    language = locale.language
    buckets = []
    for rule in profile.default_tax_rules:
        label = LABEL_REGISTRY.get(language, rule.label_key)
        buckets.append(TaxBucket(
            code=rule.code,
            label=f"{label} ({_rate_text(rule.rate_percent, language)})",
            rate_percent=rule.rate_percent,
            taxable_base=taxable_base,
            amount=pct(rule.rate_percent, taxable_base),
        ))
    return tuple(buckets)


def _rate_text(rate: Decimal, language) -> str:  # noqa: ANN001
    from docloom.core.locale.formatting import format_rate
    from docloom.core.locale.enums import Locale
    # A representative locale per language for the printed rate in the bucket label.
    loc = {"en": Locale.EN_US, "fr-CA": Locale.FR_CA, "fr-FR": Locale.FR_FR}[str(language)]
    return format_rate(rate, loc)


# ─────────────────────────────────────────────────────────────────────────────
# Sampler
# ─────────────────────────────────────────────────────────────────────────────
class InvoiceSampler:
    """Deterministic invoice generation from a catalogue. No API keys."""

    def __init__(
        self,
        catalogue: Catalogue | None = None,
        *,
        max_line_items: int = 8_000,
        goods_receipt: bool = False,
        selection: Selection | None = None,
    ) -> None:
        self._catalogue = catalogue or SeedCatalogue()
        self._max_line_items = max_line_items
        # `goods_receipt=` predates selections and is kept as the shorthand it
        # became in tests; it is the same constraint, so it folds in rather than
        # living alongside as a second way to say one thing.
        base = selection or Selection()
        self._selection = replace(base, goods_receipt=base.goods_receipt or goods_receipt)
        self._goods_receipt = self._selection.goods_receipt
        self._composition: tuple[str, Composition] | None = None

    @property
    def selection(self) -> Selection:
        return self._selection

    def composition(self, run_id: str) -> Composition:
        """The resolved pools for this run, computed once and reused.

        Cached on the run id rather than at construction because a sampler is
        built before the run it will serve, and the reproducible "use N of them"
        subset is a function of that id.
        """
        if self._composition is None or self._composition[0] != run_id:
            self._composition = (run_id, resolve(self._selection, self._catalogue, run_id))
        return self._composition[1]

    def prepare(self, run_id: str) -> None:
        """Resolve this run's selection up front, so an impossible constraint
        stops the run instead of failing every unit in turn."""
        self.composition(run_id)

    def _draw_issue_date(self, rng: Random, business_type: BusinessType) -> date:
        """Uniform over the slice's issue-date range, or the default window, but
        never before the business type's era: an AI-platform vendor has no 2019
        invoices, so its window floor rises to EARLIEST_ISSUE_DATE. A window that
        ends before that era can't produce the type at all — composition.resolve
        drops it up front, so here the floor never exceeds the window end."""
        start, end = self._selection.issue_date_range or (
            _DEFAULT_ISSUE_START, _DEFAULT_ISSUE_END)
        floor = EARLIEST_ISSUE_DATE.get(business_type)
        if floor is not None and floor > start:
            start = min(floor, end)
        return start + timedelta(days=rng.randint(0, (end - start).days))

    def generate(self, run_id: str, index: int) -> GoldenInvoice:
        rng = Random(stable_seed(run_id, index))
        composition = self.composition(run_id)
        company = composition.roster.choose(rng)
        spec = self._catalogue.spec_for(company)
        products = spec.products
        if self._goods_receipt:
            # A receiver signs for things that were delivered, so every line has
            # to be a physical good — the record validates this, and filtering
            # here is what keeps that validator from ever firing in a real run.
            products = tuple(p for p in spec.products if p.kind in GOODS_KINDS) or spec.products

        language = company.locale.language
        # Drawn before the lines so a subscription's billing period can be bound to
        # it (arrears — the period ends on/before issue). Every other date on the
        # document derives from this one, so the run's date range governs them all.
        issue = self._draw_issue_date(rng, company.business_type)
        n = min(rng.randint(spec.line_count_low, spec.line_count_high), self._max_line_items)
        lines = tuple(
            _build_line(rng, i + 1, product, language, issue)
            for i, product in enumerate(_draw_products(rng, products, n))
        )
        if company.business_type is BusinessType.TELECOM:
            lines = _telecom_grouping(rng, lines, language)   # subscriber → category → event
        subtotal = sum_money([li.extended_amount for li in lines])

        scheme, timing, discount = _discount(rng, subtotal)
        shipping = money(Decimal(str(rng.uniform(8, 60)))) if rng.random() < 0.25 else ZERO
        deposit = money(subtotal * Decimal(str(rng.uniform(0.1, 0.3)))) if rng.random() < 0.15 else ZERO

        if timing is DiscountTiming.PRE_TAX:
            taxable_base = money(subtotal - discount + shipping)
        else:
            taxable_base = money(subtotal + shipping)

        buckets = _tax_buckets(company.jurisdiction, taxable_base, company.locale)
        tax_total = sum_money([b.amount for b in buckets])
        grand = money(subtotal - discount + shipping + tax_total - deposit)

        return GoldenInvoice(
            invoice_id=f"inv_{index:08d}",
            run_id=run_id,
            invoice_index=index,
            seed=stable_seed(run_id, index),
            invoice_number=f"INV-{issue.year}-{index:06d}",
            issue_date=issue,
            due_date=issue + timedelta(days=30),
            issuer=company.party,
            recipient=Party(party_id=f"cust_{rng.randint(1000, 9999)}",
                            name=rng.choice(_RECIPIENTS)),
            business_type=company.business_type,
            product_category=company.product_category,
            llm_niche=company.llm_niche,
            locale=company.locale,
            jurisdiction=company.jurisdiction,
            currency=company.currency,
            line_items=lines,
            tax_buckets=buckets,
            totals=InvoiceTotals(
                subtotal=subtotal, discount_total=discount, shipping_total=shipping,
                taxable_base=taxable_base, tax_total=tax_total, deposit=deposit,
                grand_total=grand,
            ),
            discount_scheme=scheme,
            discount_timing=timing,
            company_id=company.company_id,
            # Which content pool produced this document — see Catalogue.version.
            catalogue_version=getattr(self._catalogue, "version", ""),
            # A constrained slice overrides the company's usual look; otherwise
            # the company keeps it, which is what makes its invoices recognisably
            # its own.
            render_profile=company.render_profile.model_copy(
                update={"archetype": composition.archetype_for(
                    rng, company.render_profile.archetype)}
            ),
            # A goods receipt is a handwritten delivery note the customer signs,
            # so the variant carries both flags together rather than leaving a
            # receipt block that no archetype would render.
            condition=composition.condition_for(rng),
            wear=composition.wear_for(rng),
            goods_receipt=self._goods_receipt,
            received_date=(issue + timedelta(days=rng.randint(0, 6))
                           if self._goods_receipt else None),
        )
