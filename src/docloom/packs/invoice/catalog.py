"""Invoice catalogue — the content the sampler draws from.

A catalogue answers three questions the sampler needs and the kernel does not
care about: which companies issue invoices (a weighted roster, so some issue far
more than others), what each business type sells (product pools with prices,
codes, and billing shapes), and how a company's invoices look (a render profile
fixed per company, so a vendor's documents are visually consistent).

Two implementations share the :class:`Catalogue` interface:

* :class:`SeedCatalogue` — procedural, built in, **no API keys**. Produces valid,
  varied invoices with modest lexical diversity. This is what makes docloom run
  end to end with nothing but ``pip install``.
* a future file catalogue — the rich, LLM-generated content, produced offline by
  the catalogue runner and swapped in behind this same interface without
  touching the sampler.

Everything here is deterministic given a seed: the roster, each company's
identity and look, and the product pools are all reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from random import Random
from typing import Protocol, runtime_checkable

from docloom.core.locale.enums import Currency, Locale
from docloom.packs.invoice.enums import (
    BillingModel,
    BusinessType,
    CodeSystem,
    LineItemKind,
    UsageUnit,
)
from docloom.packs.invoice.jurisdictions import Jurisdiction
from docloom.packs.invoice.record import Party, RenderProfile, TaxRegistration


# ─────────────────────────────────────────────────────────────────────────────
# Product and business shapes
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ProductTemplate:
    """One line an invoice might contain, before quantities and prices are drawn."""

    description: str
    price_low: Decimal
    price_high: Decimal
    kind: LineItemKind = LineItemKind.PRODUCT
    billing_model: BillingModel = BillingModel.PER_UNIT
    code_system: CodeSystem = CodeSystem.SKU
    code_prefix: str = "SKU"
    usage_unit: UsageUnit = UsageUnit.NONE


@dataclass(frozen=True, slots=True)
class BusinessSpec:
    """How one business type bills: its products, archetype, and line-count range."""

    business_type: BusinessType
    archetype: str
    products: tuple[ProductTemplate, ...]
    line_count_low: int = 3
    line_count_high: int = 12


@dataclass(frozen=True, slots=True)
class Company:
    """A recurring issuer. Its identity and look are fixed, so all its invoices
    read as the same vendor; only the line items vary per invoice."""

    company_id: str
    name: str
    business_type: BusinessType
    jurisdiction: Jurisdiction
    locale: Locale
    currency: Currency
    party: Party
    render_profile: RenderProfile
    weight: float = 1.0


class CompanyRoster:
    """Weighted selection over companies — some issue far more invoices."""

    def __init__(self, companies: list[Company]) -> None:
        if not companies:
            raise ValueError("a roster needs at least one company")
        self._companies = companies
        total = sum(c.weight for c in companies)
        if total <= 0:
            raise ValueError("company weights must sum to a positive number")
        acc = 0.0
        self._cumulative: list[float] = []
        for c in companies:
            acc += c.weight / total
            self._cumulative.append(acc)

    def __len__(self) -> int:
        return len(self._companies)

    @property
    def companies(self) -> list[Company]:
        return self._companies

    def choose(self, rng: Random) -> Company:
        point = rng.random()
        for company, ceiling in zip(self._companies, self._cumulative, strict=True):
            if point < ceiling:
                return company
        return self._companies[-1]


@runtime_checkable
class Catalogue(Protocol):
    def roster(self) -> CompanyRoster: ...
    def business_spec(self, business_type: BusinessType) -> BusinessSpec: ...


# ─────────────────────────────────────────────────────────────────────────────
# Seed data — compact but real; extended by adding rows, not code.
# ─────────────────────────────────────────────────────────────────────────────
def _d(v: str) -> Decimal:
    return Decimal(v)

_PRODUCTS: dict[BusinessType, tuple[ProductTemplate, ...]] = {
    BusinessType.RETAIL: (
        ProductTemplate("Stainless steel hex bolt, M8 x 40mm", _d("0.30"), _d("0.90"),
                        code_prefix="HB", code_system=CodeSystem.SKU),
        ProductTemplate("Nitrile gasket set, 3-inch flange", _d("6.00"), _d("14.00"),
                        code_prefix="GS"),
        ProductTemplate("LED work light, 20W rechargeable", _d("28.00"), _d("65.00"),
                        code_prefix="WL"),
        ProductTemplate("Heavy-duty utility gloves, pair", _d("4.50"), _d("12.00"),
                        code_prefix="GL"),
        ProductTemplate("Cordless drill driver, 18V", _d("89.00"), _d("199.00"),
                        code_prefix="DR", code_system=CodeSystem.MPN),
    ),
    BusinessType.WHOLESALE: (
        ProductTemplate("Corrugated shipping carton, 12x12x8 (bundle of 25)",
                        _d("18.00"), _d("34.00"), code_prefix="CT", code_system=CodeSystem.UNSPSC),
        ProductTemplate("Industrial pallet wrap, 18-inch (roll)", _d("9.00"), _d("22.00"),
                        code_prefix="PW", code_system=CodeSystem.UNSPSC),
        ProductTemplate("Thermal receipt paper, 80mm (case of 50)", _d("40.00"), _d("70.00"),
                        code_prefix="RP", code_system=CodeSystem.UNSPSC),
    ),
    BusinessType.B2B_SAAS: (
        ProductTemplate("Growth plan — monthly subscription", _d("199.00"), _d("999.00"),
                        kind=LineItemKind.SUBSCRIPTION, billing_model=BillingModel.SUBSCRIPTION,
                        code_system=CodeSystem.NONE, code_prefix="PLN"),
        ProductTemplate("Additional user seats", _d("12.00"), _d("35.00"),
                        kind=LineItemKind.SEAT, billing_model=BillingModel.SEAT_BASED,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.SEATS),
        ProductTemplate("Premium support add-on", _d("150.00"), _d("500.00"),
                        kind=LineItemKind.FEE, billing_model=BillingModel.FLAT_RATE,
                        code_system=CodeSystem.NONE),
    ),
    BusinessType.AI_PLATFORM: (
        ProductTemplate("Input tokens (millions)", _d("0.10"), _d("0.40"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.TOKENS_INPUT),
        ProductTemplate("Output tokens (millions)", _d("0.20"), _d("0.90"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.TOKENS_OUTPUT),
        ProductTemplate("API requests", _d("0.0008"), _d("0.0025"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.GRADUATED_TIER,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.API_CALLS),
        ProductTemplate("GPU inference hours", _d("1.20"), _d("4.50"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.GPU_HOURS),
    ),
    BusinessType.AUTO_REPAIR: (
        ProductTemplate("Diagnostic and repair labour", _d("95.00"), _d("165.00"),
                        kind=LineItemKind.LABOUR, billing_model=BillingModel.HOURLY_LABOUR,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.HOURS),
        ProductTemplate("Ceramic brake pad set, front", _d("55.00"), _d("140.00"),
                        kind=LineItemKind.PART, code_prefix="BP", code_system=CodeSystem.MPN),
        ProductTemplate("Synthetic oil filter", _d("12.00"), _d("28.00"),
                        kind=LineItemKind.PART, code_prefix="OF", code_system=CodeSystem.MPN),
        ProductTemplate("Shop supplies & disposal", _d("15.00"), _d("40.00"),
                        kind=LineItemKind.SURCHARGE, billing_model=BillingModel.FEE_SCHEDULE,
                        code_system=CodeSystem.NONE),
    ),
    BusinessType.ACCOUNTING: (
        ProductTemplate("Federal tax preparation", _d("300.00"), _d("1200.00"),
                        kind=LineItemKind.SERVICE, billing_model=BillingModel.FLAT_RATE,
                        code_system=CodeSystem.NONE),
        ProductTemplate("Bookkeeping — monthly", _d("250.00"), _d("800.00"),
                        kind=LineItemKind.SERVICE, billing_model=BillingModel.SUBSCRIPTION,
                        code_system=CodeSystem.NONE),
        ProductTemplate("Advisory consultation", _d("150.00"), _d("400.00"),
                        kind=LineItemKind.LABOUR, billing_model=BillingModel.HOURLY_LABOUR,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.HOURS),
    ),
    BusinessType.TELECOM: (
        ProductTemplate("Mobile data", _d("0.01"), _d("0.05"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.MEGABYTES),
        ProductTemplate("Voice minutes", _d("0.02"), _d("0.08"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.METERED_USAGE,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.MINUTES),
        ProductTemplate("Text messages", _d("0.05"), _d("0.15"),
                        kind=LineItemKind.USAGE, billing_model=BillingModel.PER_UNIT,
                        code_system=CodeSystem.NONE, usage_unit=UsageUnit.MESSAGES),
    ),
}

# Fallback pool for any business type without a bespoke one yet.
_GENERIC = (
    ProductTemplate("Professional services", _d("100.00"), _d("500.00"),
                    kind=LineItemKind.SERVICE, billing_model=BillingModel.FLAT_RATE,
                    code_system=CodeSystem.NONE),
)

_ARCHETYPE = {BusinessType.TELECOM: "telecom-itemized-37"}   # else the flat archetype
_FLAT_ARCHETYPE = "meta-sidebar-01"

_NAME_PARTS = (
    "Northwind", "Cedar", "Ironclad", "Bluepeak", "Vantage", "Meridian", "Aurora",
    "Copperline", "Summit", "Harbor", "Crescent", "Bramble", "Halcyon", "Foxglove",
    "Granite", "Willowmere", "Tanager", "Ashford", "Beacon", "Kestrel",
)
_NAME_TAIL = {
    Jurisdiction.US: ("Inc.", "LLC", "Co.", "Corp."),
    Jurisdiction.CA_ON: ("Inc.", "Ltd."),
    Jurisdiction.CA_QC: ("inc.", "ltée"),
    Jurisdiction.CA_BC: ("Inc.", "Ltd."),
    Jurisdiction.CA_AB: ("Inc.", "Ltd."),
    Jurisdiction.GB: ("Ltd", "PLC"),
    Jurisdiction.FR: ("SARL", "SAS", "SA"),
}
_TYPEFACES = ("Inter", "Source Serif Pro", "IBM Plex Sans", "Georgia", "Libre Franklin",
              "Merriweather", "Work Sans", "Lora", "Rubik", "Spectral")
_ACCENTS = ("#2D6A4F", "#1F4E5F", "#7A3B2E", "#3B4C7A", "#5B3A70", "#8A6D1C",
            "#2B2B2B", "#134E4A", "#7C2D12", "#1E3A5F")
_META_POSITIONS = ("top-left", "top-right", "top-row", "left-rail", "right-rail", "split")
_TOTALS_STYLES = ("right-stack", "full-width", "boxed", "inline")
_TABLE_STYLES = ("rules", "bordered", "zebra", "borderless")
_LOGO_LOCKUPS = ("left", "center", "stacked")
_VOCAB = {
    Locale.EN_US: ("standard", "caps", "unit_cost", "item", "product"),
    Locale.EN_CA: ("standard", "caps", "unit_cost", "item"),
    Locale.EN_GB: ("standard", "unit_cost", "item"),
    Locale.FR_CA: ("standard", "titre", "prix_unite"),
    Locale.FR_FR: ("standard", "reference", "with_vat_col"),
}
_CITY = {
    Jurisdiction.US: ("New York, NY", "Austin, TX", "Denver, CO", "Chicago, IL"),
    Jurisdiction.CA_ON: ("Toronto, ON", "Ottawa, ON"),
    Jurisdiction.CA_QC: ("Montréal, QC", "Québec, QC"),
    Jurisdiction.CA_BC: ("Vancouver, BC",),
    Jurisdiction.CA_AB: ("Calgary, AB",),
    Jurisdiction.GB: ("London", "Manchester", "Bristol"),
    Jurisdiction.FR: ("Paris", "Lyon", "Bordeaux"),
}


class SeedCatalogue:
    """Procedural, key-free catalogue. Deterministic from a build seed."""

    def __init__(
        self,
        *,
        anchor_count: int = 50_000,
        french_company_count: int = 5,
        french_each: int = 1_000,
        other_count: int = 100,
        other_total: int = 195_000,
        seed: int = 0,
    ) -> None:
        rng = Random(seed)
        companies: list[Company] = []

        # One anchor company issues the largest share.
        companies.append(self._make_company(rng, "anchor", Jurisdiction.US, Locale.EN_US,
                                             weight=anchor_count))
        # A handful of French companies (fr-CA and fr-FR both represented).
        for i in range(french_company_count):
            juris, locale = ((Jurisdiction.FR, Locale.FR_FR) if i % 2 == 0
                             else (Jurisdiction.CA_QC, Locale.FR_CA))
            companies.append(self._make_company(rng, f"fr{i}", juris, locale,
                                                 weight=french_each))
        # The remaining roster, spread across jurisdictions, sharing the remainder.
        spread = [Jurisdiction.US, Jurisdiction.CA_ON, Jurisdiction.GB, Jurisdiction.CA_BC]
        per = other_total / max(other_count, 1)
        for i in range(other_count):
            juris = spread[i % len(spread)]
            locale = Locale.EN_GB if juris is Jurisdiction.GB else (
                Locale.EN_CA if str(juris).startswith("CA") else Locale.EN_US)
            companies.append(self._make_company(rng, f"co{i}", juris, locale, weight=per))

        self._roster = CompanyRoster(companies)

    def roster(self) -> CompanyRoster:
        return self._roster

    def business_spec(self, business_type: BusinessType) -> BusinessSpec:
        products = _PRODUCTS.get(business_type, _GENERIC)
        archetype = _ARCHETYPE.get(business_type, _FLAT_ARCHETYPE)
        low, high = (200, 1500) if business_type is BusinessType.TELECOM else (3, 12)
        return BusinessSpec(business_type, archetype, products, low, high)

    # ── company construction ────────────────────────────────────────────────
    def _make_company(self, rng: Random, cid: str, juris: Jurisdiction,
                      locale: Locale, *, weight: float) -> Company:
        business_type = rng.choice(list(_PRODUCTS))
        currency = {Jurisdiction.GB: Currency.GBP, Jurisdiction.FR: Currency.EUR}.get(
            juris, Currency.CAD if str(juris).startswith("CA") else Currency.USD)
        name = f"{rng.choice(_NAME_PARTS)} {rng.choice(_NAME_PARTS)} {rng.choice(_NAME_TAIL[juris])}"
        slug = name.lower().replace(" ", "").replace(".", "")[:20]

        party = Party(
            party_id=cid,
            name=name,
            address_lines=(f"{rng.randint(10, 9999)} {rng.choice(_NAME_PARTS)} Street",),
            city=rng.choice(_CITY[juris]),
            phone=f"+1 {rng.randint(200, 989)}-555-{rng.randint(1000, 9999):04d}",
            email=f"billing@{slug}.example",
            website=f"www.{slug}.example",
            registrations=self._registrations(rng, juris),
        )
        archetype = _ARCHETYPE.get(business_type, _FLAT_ARCHETYPE)
        profile = RenderProfile(
            archetype=archetype,
            meta_position=rng.choice(_META_POSITIONS),
            totals_style=rng.choice(_TOTALS_STYLES),
            table_style=rng.choice(_TABLE_STYLES),
            column_vocabulary=rng.choice(_VOCAB[locale]),
            typeface=rng.choice(_TYPEFACES),
            accent_color=rng.choice(_ACCENTS),
            logo_lockup=rng.choice(_LOGO_LOCKUPS),
            has_logo=rng.random() > 0.2,   # ~20% text-only, like real invoices
        )
        return Company(company_id=cid, name=name, business_type=business_type,
                       jurisdiction=juris, locale=locale, currency=currency,
                       party=party, render_profile=profile, weight=weight)

    def _registrations(self, rng: Random, juris: Jurisdiction) -> tuple[TaxRegistration, ...]:
        """Format-valid but fictional issuer tax registrations."""
        if juris is Jurisdiction.FR:
            siret = "".join(str(rng.randint(0, 9)) for _ in range(14))
            tva = f"FR{rng.randint(10, 99)}{siret[:9]}"
            return (TaxRegistration(kind="SIRET", value=siret),
                    TaxRegistration(kind="TVA_INTRA", value=tva))
        if juris is Jurisdiction.GB:
            return (TaxRegistration(kind="VAT", value=f"GB {rng.randint(100, 999)} "
                                                      f"{rng.randint(1000, 9999)} "
                                                      f"{rng.randint(10, 99)}"),)
        if str(juris).startswith("CA"):
            gst = f"{rng.randint(100000000, 999999999)}RT0001"
            regs = [TaxRegistration(kind="GST", value=gst)]
            if juris is Jurisdiction.CA_QC:
                regs.append(TaxRegistration(kind="QST",
                                            value=f"{rng.randint(1000000000, 9999999999)}TQ0001"))
            return tuple(regs)
        return (TaxRegistration(kind="EIN", value=f"{rng.randint(10, 99)}-"
                                                  f"{rng.randint(1000000, 9999999)}"),)
