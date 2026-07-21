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

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from random import Random

from docloom.core.money import ZERO, money, pct, sum_money
from docloom.core.pipeline.source import stable_seed
from docloom.packs.invoice.catalog import Catalogue, Company, ProductTemplate, SeedCatalogue
from docloom.packs.invoice.enums import (
    BillingModel,
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

_BASE_DATE = date(2026, 1, 1)
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


def _period(rng: Random) -> tuple[date, date]:
    start = _BASE_DATE + timedelta(days=rng.randint(0, 300))
    return start, start + timedelta(days=29)


# ─────────────────────────────────────────────────────────────────────────────
# Per-billing-model line construction — each returns an exact LineItem
# ─────────────────────────────────────────────────────────────────────────────
def _build_line(rng: Random, line_no: int, product: ProductTemplate) -> LineItem:
    unit_price = _price(rng, product.price_low, product.price_high)
    code = f"{product.code_prefix}-{rng.randint(1000, 99999)}" if product.code_prefix else None
    common = {
        "line_no": line_no, "description": product.description, "kind": product.kind,
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
        start, end = _period(rng)
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


def _graduated_line(rng: Random, common: dict, product: ProductTemplate) -> LineItem:
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

    def __init__(self, catalogue: Catalogue | None = None, *, max_line_items: int = 8_000) -> None:
        self._catalogue = catalogue or SeedCatalogue()
        self._max_line_items = max_line_items

    def generate(self, run_id: str, index: int) -> GoldenInvoice:
        rng = Random(stable_seed(run_id, index))
        company = self._catalogue.roster().choose(rng)
        spec = self._catalogue.business_spec(company.business_type)

        n = min(rng.randint(spec.line_count_low, spec.line_count_high), self._max_line_items)
        lines = tuple(
            _build_line(rng, i + 1, rng.choice(spec.products)) for i in range(n)
        )
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

        issue = _BASE_DATE + timedelta(days=rng.randint(0, 330))
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
            render_profile=company.render_profile,
        )
