"""Locale kernel: language/locale vocabularies, formatting tables, label registry."""

from docsynth.core.locale.enums import LOCALE_LANGUAGE, Currency, Language, Locale
from docsynth.core.locale.formatting import (
    CURRENCY_SYMBOL,
    LOCALE_FORMATS,
    LocaleFormat,
    format_amount,
    format_date,
    format_date_long,
    format_for,
    format_quantity,
    format_rate,
    language_for,
)
from docsynth.core.locale.labels import LabelRegistry

__all__ = [
    "CURRENCY_SYMBOL",
    "LOCALE_FORMATS",
    "LOCALE_LANGUAGE",
    "Currency",
    "LabelRegistry",
    "Language",
    "Locale",
    "LocaleFormat",
    "format_amount",
    "format_date",
    "format_date_long",
    "format_for",
    "format_quantity",
    "format_rate",
    "language_for",
]
