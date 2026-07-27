"""Locale-aware formatting — table-driven, not branch-driven.

Every convention lives in a :class:`LocaleFormat` row in :data:`LOCALE_FORMATS`.
Adding a locale is one entry; no formatting function changes. That matters
because the alternative — ``if language is Language.EN: ... else: ...`` in four
separate functions — makes each new locale shotgun surgery across the module,
and the branches grow without bound.

Deliberately hand-rolled rather than delegated to ``babel`` or the ``locale``
module: these strings are the extraction target, so their exact byte form must
not shift with a system locale or a library upgrade.

    en-US   $1,234.56        07/15/2026
    en-CA   $1,234.56        2026-07-15
    en-GB   £1,234.56        15/07/2026
    fr-CA   1 234,56 $       15 juillet 2026
    fr-FR   1 234,56 €       15/07/2026

The French locales differ from the English ones in three ways at once — narrow
no-break space grouping, comma decimal mark, trailing symbol — which is useful
adversarial material for an extractor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from docsynth.core.locale.enums import Currency, Language, Locale
from docsynth.core.money import money

# U+202F NARROW NO-BREAK SPACE — the correct French group and currency
# separator. Written as an escape, never as a literal: an invisible character in
# source is trivially corrupted by editors, copy-paste, and file rewrites, and
# the failure is silent because it still *looks* like a space. This constant was
# in fact a plain 0x20 at one point, which no test caught until the codepoint
# was asserted directly (see test_i18n.py::test_nnbsp_is_really_u202f).
NNBSP: Final = "\u202f"

CURRENCY_SYMBOL: dict[Currency, str] = {
    Currency.USD: "$",
    Currency.CAD: "$",
    Currency.GBP: "£",
    Currency.EUR: "€",
}

ENGLISH_MONTHS: Final[tuple[str, ...]] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

FRENCH_MONTHS: Final[tuple[str, ...]] = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


@dataclass(frozen=True, slots=True)
class LocaleFormat:
    """How one locale shapes numbers and dates.

    Date patterns use ``{D} {DD} {M} {MM} {MONTH} {YYYY}`` tokens rather than
    ``strftime``, because ``strftime`` month names come from the process locale
    and would vary by host.
    """

    locale: Locale
    decimal_mark: str
    group_separator: str
    symbol_leads: bool          # "$1,234.56" vs "1 234,56 $"
    symbol_gap: str             # separator between amount and symbol
    negative_prefix: str
    percent_gap: str            # "" -> "20%",  NNBSP -> "20 %"
    date_pattern: str
    long_date_pattern: str
    month_names: tuple[str, ...]


LOCALE_FORMATS: dict[Locale, LocaleFormat] = {
    Locale.EN_US: LocaleFormat(
        locale=Locale.EN_US,
        decimal_mark=".", group_separator=",",
        symbol_leads=True, symbol_gap="", negative_prefix="-", percent_gap="",
        date_pattern="{MM}/{DD}/{YYYY}", long_date_pattern="{MONTH} {D}, {YYYY}",
        month_names=ENGLISH_MONTHS,
    ),
    Locale.EN_CA: LocaleFormat(
        locale=Locale.EN_CA,
        decimal_mark=".", group_separator=",",
        symbol_leads=True, symbol_gap="", negative_prefix="-", percent_gap="",
        date_pattern="{YYYY}-{MM}-{DD}", long_date_pattern="{MONTH} {D}, {YYYY}",
        month_names=ENGLISH_MONTHS,
    ),
    Locale.EN_GB: LocaleFormat(
        locale=Locale.EN_GB,
        decimal_mark=".", group_separator=",",
        symbol_leads=True, symbol_gap="", negative_prefix="-", percent_gap="",
        date_pattern="{DD}/{MM}/{YYYY}", long_date_pattern="{D} {MONTH} {YYYY}",
        month_names=ENGLISH_MONTHS,
    ),
    Locale.FR_CA: LocaleFormat(
        locale=Locale.FR_CA,
        decimal_mark=",", group_separator=NNBSP,
        symbol_leads=False, symbol_gap=NNBSP, negative_prefix=f"-{NNBSP}",
        percent_gap=NNBSP,
        date_pattern="{D} {MONTH} {YYYY}", long_date_pattern="{D} {MONTH} {YYYY}",
        month_names=FRENCH_MONTHS,
    ),
    Locale.FR_FR: LocaleFormat(
        locale=Locale.FR_FR,
        decimal_mark=",", group_separator=NNBSP,
        symbol_leads=False, symbol_gap=NNBSP, negative_prefix=f"-{NNBSP}",
        percent_gap=NNBSP,
        date_pattern="{DD}/{MM}/{YYYY}", long_date_pattern="{D} {MONTH} {YYYY}",
        month_names=FRENCH_MONTHS,
    ),
}


def format_for(locale: Locale) -> LocaleFormat:
    try:
        return LOCALE_FORMATS[locale]
    except KeyError as exc:  # pragma: no cover - guarded by test_locale_coverage
        raise KeyError(f"no LocaleFormat registered for {locale}") from exc


def language_for(locale: Locale) -> Language:
    """Which label dictionary this locale prints."""
    return locale.language


def _group(digits: str, separator: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(reversed(digits)):
        if i and i % 3 == 0:
            out.append(separator)
        out.append(ch)
    return "".join(reversed(out))


def _render_date(pattern: str, value: date, months: tuple[str, ...]) -> str:
    """Substitute date tokens. Two-character tokens are replaced before their
    one-character counterparts so ``{DD}`` is not partially consumed."""
    return (
        pattern.replace("{DD}", f"{value.day:02d}")
        .replace("{MM}", f"{value.month:02d}")
        .replace("{MONTH}", months[value.month - 1])
        .replace("{YYYY}", str(value.year))
        .replace("{D}", str(value.day))
        .replace("{M}", str(value.month))
    )


def format_amount(
    value: Decimal,
    currency: Currency,
    locale: Locale,
    *,
    with_symbol: bool = True,
) -> str:
    """Format a monetary amount exactly as it is printed."""
    fmt = format_for(locale)
    q = money(value)
    negative = q < 0
    digits, _, frac = str(abs(q)).partition(".")

    body = f"{_group(digits, fmt.group_separator)}{fmt.decimal_mark}{frac}"
    if with_symbol:
        symbol = CURRENCY_SYMBOL[currency]
        body = (
            f"{symbol}{fmt.symbol_gap}{body}"
            if fmt.symbol_leads
            else f"{body}{fmt.symbol_gap}{symbol}"
        )
    return f"{fmt.negative_prefix}{body}" if negative else body


def format_rate(rate_percent: Decimal, locale: Locale) -> str:
    """``9.975`` -> ``9.975%`` (en) or ``9,975 %`` (fr). Trailing zeros trimmed,
    so 20.000 prints as 20."""
    fmt = format_for(locale)
    text = format(rate_percent.normalize(), "f").replace(".", fmt.decimal_mark)
    return f"{text}{fmt.percent_gap}%"


def format_date(value: date, locale: Locale) -> str:
    fmt = format_for(locale)
    return _render_date(fmt.date_pattern, value, fmt.month_names)


def format_date_long(value: date, locale: Locale) -> str:
    fmt = format_for(locale)
    return _render_date(fmt.long_date_pattern, value, fmt.month_names)


def format_quantity(value: Decimal, locale: Locale) -> str:
    """Quantities print without a currency symbol, and drop a trailing ``.00``."""
    fmt = format_for(locale)
    return format(value.normalize(), "f").replace(".", fmt.decimal_mark)
