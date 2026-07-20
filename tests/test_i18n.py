"""Localisation tests, with an emphasis on extensibility guards.

Adding a locale or a language should be a table entry, never a code change.
These tests enforce that: every ``Locale`` must resolve to a format row, every
format row's language must have a complete label dictionary, and every locale
must actually render an invoice. Add an enum member without its table entry and
the suite tells you exactly what is missing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from docloom.core import Currency, Jurisdiction, Locale, get_pack, render_record
from docloom.core.locale import (
    LOCALE_FORMATS,
    format_amount,
    format_date,
    format_date_long,
    format_quantity,
    format_rate,
    language_for,
)
from docloom.core.locale.formatting import NNBSP
from docloom.packs.invoice.labels import COLUMN_VOCABULARIES, LABEL_REGISTRY, LABELS
from tests.factories import invoice, simple_lines

# The exact separator French locales use. Asserted by codepoint because it is
# visually indistinguishable from a plain space.
NARROW_NBSP = "\u202f"


# ─────────────────────────────────────────────────────────────────────────────
# Registry completeness — the guards that make extension safe
# ─────────────────────────────────────────────────────────────────────────────

def test_nnbsp_is_really_u202f() -> None:
    """Regression: this constant was once a plain 0x20 and nothing caught it,
    because both the code and the tests used the same wrong character."""
    assert ord(NNBSP) == 0x202F


def test_every_locale_has_a_format_row() -> None:
    missing = [loc for loc in Locale if loc not in LOCALE_FORMATS]
    assert not missing, f"Locale members without a LocaleFormat: {missing}"


def test_every_locale_resolves_to_a_language_with_labels() -> None:
    for loc in Locale:
        lang = language_for(loc)
        assert lang in LABELS, f"{loc} -> {lang} has no label dictionary"
        assert lang in COLUMN_VOCABULARIES, f"{loc} -> {lang} has no column vocabularies"


def test_label_tables_are_complete_and_consistent() -> None:
    """Every language defines the same key set, with no blanks."""
    LABEL_REGISTRY.validate()


def test_unknown_label_key_raises() -> None:
    with pytest.raises(KeyError, match="no label"):
        LABEL_REGISTRY.get(language_for(Locale.EN_US), "not_a_real_key")


@pytest.mark.parametrize("loc", list(Locale))
def test_every_locale_renders_an_invoice(loc: Locale) -> None:
    """The strongest extensibility guard: a new Locale must render without any
    code change beyond its table entry."""
    currency = {
        Locale.EN_US: Currency.USD,
        Locale.EN_CA: Currency.CAD,
        Locale.EN_GB: Currency.GBP,
        Locale.FR_CA: Currency.CAD,
        Locale.FR_FR: Currency.EUR,
    }[loc]
    jurisdiction = {
        Locale.EN_US: Jurisdiction.US,
        Locale.EN_CA: Jurisdiction.CA_ON,
        Locale.EN_GB: Jurisdiction.GB,
        Locale.FR_CA: Jurisdiction.CA_QC,
        Locale.FR_FR: Jurisdiction.FR,
    }[loc]
    out = render_record(get_pack("invoice"),
                        invoice(simple_lines(), locale=loc,
                                jurisdiction=jurisdiction, currency=currency))
    assert out.startswith("<!doctype html>")
    assert f'lang="{loc.value}"' in out


# ─────────────────────────────────────────────────────────────────────────────
# Formatting conventions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("loc", "currency", "expected"),
    [
        (Locale.EN_US, Currency.USD, "$1,234,567.50"),
        (Locale.EN_CA, Currency.CAD, "$1,234,567.50"),
        (Locale.EN_GB, Currency.GBP, "£1,234,567.50"),
        (Locale.FR_CA, Currency.CAD, f"1{NARROW_NBSP}234{NARROW_NBSP}567,50{NARROW_NBSP}$"),
        (Locale.FR_FR, Currency.EUR, f"1{NARROW_NBSP}234{NARROW_NBSP}567,50{NARROW_NBSP}€"),
    ],
)
def test_amount_formatting(loc: Locale, currency: Currency, expected: str) -> None:
    assert format_amount(D("1234567.5"), currency, loc) == expected


def test_negative_amount_formatting() -> None:
    assert format_amount(D("-42.00"), Currency.USD, Locale.EN_US) == "-$42.00"
    assert (
        format_amount(D("-42.00"), Currency.EUR, Locale.FR_FR)
        == f"-{NARROW_NBSP}42,00{NARROW_NBSP}€"
    )


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (Locale.EN_US, "07/15/2026"),
        (Locale.EN_CA, "2026-07-15"),
        (Locale.EN_GB, "15/07/2026"),
        (Locale.FR_CA, "15 juillet 2026"),
        (Locale.FR_FR, "15/07/2026"),
    ],
)
def test_date_formatting(loc: Locale, expected: str) -> None:
    assert format_date(date(2026, 7, 15), loc) == expected


def test_en_gb_and_en_us_differ_despite_sharing_a_language() -> None:
    """The reason Locale exists at all. Before the split, region was proxied
    through currency, which was fragile and wrong for any en-GB invoice not
    priced in GBP."""
    assert language_for(Locale.EN_GB) == language_for(Locale.EN_US)
    assert format_date(date(2026, 7, 15), Locale.EN_GB) != format_date(
        date(2026, 7, 15), Locale.EN_US
    )


def test_long_date_formatting() -> None:
    assert format_date_long(date(2026, 7, 15), Locale.EN_US) == "July 15, 2026"
    assert format_date_long(date(2026, 7, 15), Locale.FR_FR) == "15 juillet 2026"


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (Locale.EN_US, "9.975%"),
        (Locale.FR_CA, f"9,975{NARROW_NBSP}%"),
        (Locale.FR_FR, f"20{NARROW_NBSP}%"),
    ],
)
def test_rate_formatting(loc: Locale, expected: str) -> None:
    rate = D("20.000") if loc is Locale.FR_FR else D("9.975")
    assert format_rate(rate, loc) == expected


def test_quantity_formatting_drops_trailing_zeros() -> None:
    assert format_quantity(D("12.50"), Locale.EN_US) == "12.5"
    assert format_quantity(D("12.50"), Locale.FR_FR) == "12,5"
    assert format_quantity(D("250"), Locale.EN_US) == "250"
