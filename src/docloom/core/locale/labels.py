"""Label registry — the *mechanism* for printed vocabulary.

The kernel owns how labels are stored, looked up, and validated. It owns none of
the actual words: an invoice pack supplies "Subtotal" and "TVQ", a contract pack
would supply "Governing Law" and "Effective Date". Both use this registry.

Lookups raise on an unknown key rather than returning a placeholder. A missing
translation must fail while generating, not appear as a blank table header
across a quarter of a million rendered documents.
"""

from __future__ import annotations

from collections.abc import Mapping

from docloom.core.locale.enums import Language

#: language -> label key -> printed text
LabelTables = Mapping[Language, Mapping[str, str]]

#: language -> vocabulary name -> column key -> printed header
VocabularyTables = Mapping[Language, Mapping[str, Mapping[str, str]]]


class LabelRegistry:
    """Printed vocabulary for one document pack."""

    def __init__(
        self,
        labels: LabelTables,
        vocabularies: VocabularyTables | None = None,
        *,
        reference_language: Language = Language.EN,
    ) -> None:
        self._labels = labels
        self._vocabularies = vocabularies or {}
        self._reference = reference_language

    @property
    def languages(self) -> tuple[Language, ...]:
        return tuple(self._labels)

    def get(self, language: Language, key: str) -> str:
        try:
            return self._labels[language][key]
        except KeyError as exc:
            raise KeyError(f"no label '{key}' for language {language}") from exc

    def vocabulary(self, language: Language, name: str) -> Mapping[str, str]:
        """Resolve a named column vocabulary, falling back to ``standard``.

        The fallback keeps a pack usable when a record names a vocabulary that
        a given language does not define — better a sensible default than a
        crash mid-run.
        """
        by_language = self._vocabularies.get(language, {})
        if name in by_language:
            return by_language[name]
        if "standard" in by_language:
            return by_language["standard"]
        raise KeyError(f"no vocabulary '{name}' or 'standard' for language {language}")

    def validate(self) -> None:
        """Assert every language defines the same keys, none of them blank.

        This is the guard that makes adding a language safe: supply the
        dictionary, run the suite, and it names exactly what was left out.
        """
        if self._reference not in self._labels:
            raise ValueError(f"reference language {self._reference} has no label table")

        reference = set(self._labels[self._reference])
        problems: list[str] = []

        for language, table in self._labels.items():
            if missing := reference - set(table):
                problems.append(f"{language}: missing {sorted(missing)}")
            if extra := set(table) - reference:
                problems.append(f"{language}: unexpected {sorted(extra)}")
            if blank := sorted(k for k, v in table.items() if not v.strip()):
                problems.append(f"{language}: blank values for {blank}")

        for language in self._labels:
            if self._vocabularies and language not in self._vocabularies:
                problems.append(f"{language}: no column vocabularies defined")
            elif self._vocabularies and "standard" not in self._vocabularies[language]:
                problems.append(f"{language}: no 'standard' vocabulary to fall back to")

        if problems:
            raise ValueError("label tables are inconsistent:\n  " + "\n  ".join(problems))
