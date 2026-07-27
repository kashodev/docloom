"""Jurisdiction profiles: tax model, currency, and legally mandatory issuer fields.

Canada is split by province because the tax model differs materially:
Ontario harmonises into a single HST line, Quebec prints GST/TPS and QST/TVQ as
two separate lines, British Columbia prints GST plus a provincial PST, and
Alberta has GST only. An extractor that assumes "one tax row" will fail on
Quebec and BC — which is exactly why the split is modelled rather than averaged.

France and Quebec both produce French-language invoices but are otherwise
unrelated: TVA vs TPS/TVQ, € vs $, SIRET vs GST/QST registration numbers, and a
Total HT / Total TTC totals structure that has no Quebec equivalent.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from docsynth.core.enums import Jurisdiction
from docsynth.core.locale.enums import Currency, Language


class TaxRule(BaseModel):
    """One tax line that this jurisdiction may print."""

    model_config = ConfigDict(frozen=True)

    code: str                      # stable code -> TaxBucket.code
    label_key: str                 # key into the label dictionary
    rate_percent: Decimal
    is_default: bool = True        # False for reduced/zero rates used by a subset of items


class JurisdictionProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    jurisdiction: Jurisdiction
    currency: Currency
    languages: tuple[Language, ...]        # languages an invoice here may be issued in
    tax_rules: tuple[TaxRule, ...]
    required_registrations: tuple[str, ...] = ()
    optional_registrations: tuple[str, ...] = ()
    uses_ht_ttc_totals: bool = False       # France-style Total HT / TVA / Total TTC
    requires_late_penalty_notice: bool = False   # legally mandatory in France

    @property
    def default_tax_rules(self) -> tuple[TaxRule, ...]:
        return tuple(r for r in self.tax_rules if r.is_default)


# ─────────────────────────────────────────────────────────────────────────────
# United States — single "Sales Tax" line, rate varies by state.
# Representative state rates; the sampler picks one per company.
# ─────────────────────────────────────────────────────────────────────────────
US_STATE_SALES_TAX: dict[str, Decimal] = {
    "NY": Decimal("8.875"),
    "CA": Decimal("8.750"),
    "TX": Decimal("8.250"),
    "IL": Decimal("10.250"),
    "WA": Decimal("10.250"),
    "FL": Decimal("7.000"),
    "MA": Decimal("6.250"),
    "CO": Decimal("8.310"),
    "AZ": Decimal("8.600"),
    "GA": Decimal("7.400"),
}

PROFILES: dict[Jurisdiction, JurisdictionProfile] = {
    Jurisdiction.US: JurisdictionProfile(
        jurisdiction=Jurisdiction.US,
        currency=Currency.USD,
        languages=(Language.EN,),
        tax_rules=(
            TaxRule(code="SALES_TAX", label_key="tax_sales", rate_percent=Decimal("8.875")),
        ),
        optional_registrations=("EIN",),
    ),
    # ── Canada ───────────────────────────────────────────────────────────────
    Jurisdiction.CA_ON: JurisdictionProfile(
        jurisdiction=Jurisdiction.CA_ON,
        currency=Currency.CAD,
        languages=(Language.EN, Language.FR_CA),
        tax_rules=(
            TaxRule(code="HST", label_key="tax_hst", rate_percent=Decimal("13.000")),
        ),
        required_registrations=("GST",),
    ),
    Jurisdiction.CA_QC: JurisdictionProfile(
        jurisdiction=Jurisdiction.CA_QC,
        currency=Currency.CAD,
        languages=(Language.FR_CA, Language.EN),
        tax_rules=(
            TaxRule(code="GST", label_key="tax_gst", rate_percent=Decimal("5.000")),
            TaxRule(code="QST", label_key="tax_qst", rate_percent=Decimal("9.975")),
        ),
        required_registrations=("GST", "QST"),
    ),
    Jurisdiction.CA_BC: JurisdictionProfile(
        jurisdiction=Jurisdiction.CA_BC,
        currency=Currency.CAD,
        languages=(Language.EN,),
        tax_rules=(
            TaxRule(code="GST", label_key="tax_gst", rate_percent=Decimal("5.000")),
            TaxRule(code="PST", label_key="tax_pst", rate_percent=Decimal("7.000")),
        ),
        required_registrations=("GST",),
    ),
    Jurisdiction.CA_AB: JurisdictionProfile(
        jurisdiction=Jurisdiction.CA_AB,
        currency=Currency.CAD,
        languages=(Language.EN,),
        tax_rules=(
            TaxRule(code="GST", label_key="tax_gst", rate_percent=Decimal("5.000")),
        ),
        required_registrations=("GST",),
    ),
    # ── United Kingdom ───────────────────────────────────────────────────────
    Jurisdiction.GB: JurisdictionProfile(
        jurisdiction=Jurisdiction.GB,
        currency=Currency.GBP,
        languages=(Language.EN,),
        tax_rules=(
            TaxRule(code="VAT", label_key="tax_vat", rate_percent=Decimal("20.000")),
            TaxRule(
                code="VAT_REDUCED",
                label_key="tax_vat",
                rate_percent=Decimal("5.000"),
                is_default=False,
            ),
            TaxRule(
                code="VAT_ZERO",
                label_key="tax_vat",
                rate_percent=Decimal("0.000"),
                is_default=False,
            ),
        ),
        required_registrations=("VAT",),
    ),
    # ── France ───────────────────────────────────────────────────────────────
    Jurisdiction.FR: JurisdictionProfile(
        jurisdiction=Jurisdiction.FR,
        currency=Currency.EUR,
        languages=(Language.FR_FR,),
        tax_rules=(
            TaxRule(code="TVA", label_key="tax_tva", rate_percent=Decimal("20.000")),
            TaxRule(
                code="TVA_INTER",
                label_key="tax_tva",
                rate_percent=Decimal("10.000"),
                is_default=False,
            ),
            TaxRule(
                code="TVA_REDUIT",
                label_key="tax_tva",
                rate_percent=Decimal("5.500"),
                is_default=False,
            ),
            TaxRule(
                code="TVA_SUPER_REDUIT",
                label_key="tax_tva",
                rate_percent=Decimal("2.100"),
                is_default=False,
            ),
        ),
        # SIRET and TVA intracommunautaire are legally mandatory on a French
        # invoice; omitting them would make the document formally invalid.
        required_registrations=("SIRET", "TVA_INTRA"),
        optional_registrations=("RCS", "APE"),
        uses_ht_ttc_totals=True,
        requires_late_penalty_notice=True,
    ),
}


def profile_for(jurisdiction: Jurisdiction) -> JurisdictionProfile:
    return PROFILES[jurisdiction]
