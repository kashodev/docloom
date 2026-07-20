"""Locale vocabularies — kernel-level, shared by every document pack.

Two independent axes, deliberately kept apart:

* :class:`Language` answers *which words* to print.
* :class:`Locale` answers *how numbers and dates are shaped*.

en-US and en-GB share every label but format dates differently; fr-CA and
fr-FR share number formatting but almost no vocabulary. Collapsing them into
one enum forces one of those pairs to be wrong.
"""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    """Which label vocabulary to print.

    Values are BCP-47-ish tags. A language is listed separately from its
    regional variants only when the vocabulary genuinely differs — fr-CA and
    fr-FR do (TPS/TVQ vs TVA, Rabais vs Remise), en-US and en-GB do not.
    """

    EN = "en"
    FR_CA = "fr-CA"
    FR_FR = "fr-FR"


class Locale(StrEnum):
    """Which formatting conventions to apply.

    Adding a locale is a table entry in ``docloom.core.locale.formatting``,
    never a code branch. A new English-speaking region reuses the ``EN`` label
    vocabulary and supplies only its number/date shape; a new language
    additionally supplies a label dictionary in the pack that needs it.
    """

    EN_US = "en-US"
    EN_CA = "en-CA"
    EN_GB = "en-GB"
    FR_CA = "fr-CA"
    FR_FR = "fr-FR"

    @property
    def language(self) -> Language:
        """The label vocabulary this locale prints."""
        return LOCALE_LANGUAGE[self]


# Single source of truth for the locale -> language relation. It lives beside
# both enums so no other module has to import a formatting table to resolve it.
LOCALE_LANGUAGE: dict[Locale, Language] = {
    Locale.EN_US: Language.EN,
    Locale.EN_CA: Language.EN,
    Locale.EN_GB: Language.EN,
    Locale.FR_CA: Language.FR_CA,
    Locale.FR_FR: Language.FR_FR,
}


class Currency(StrEnum):
    USD = "USD"
    CAD = "CAD"
    GBP = "GBP"
    EUR = "EUR"
