"""Quality and PII gates for catalogue content.

A catalogue is **published**, so a defect in it is far more expensive than a
defect in one run: it is copied, cached, and generated from for months. These
checks run before an artifact is written, and their results are recorded in the
manifest so the artifact ships with its own audit.

The checks exist because the failures are real, not hypothetical:

* **Empty and near-empty text** — a reasoning model returned ``content: ""`` with
  a normal 200 and real usage. Without a gate that becomes a blank product
  description in a published file.
* **Assistant preamble** — "Here is a product description: …" is the single most
  common way LLM output betrays itself, and it would print on the invoice.
* **PII** — the whole PII model rests on the artifact containing none. Structural
  avoidance does the heavy lifting (identity fields are never asked for and never
  stored), but a *generated description* can still leak an email, a phone number
  or a real-looking company suffix, so it is scanned rather than trusted.
* **Duplicates** — LLMs repeat heavily at scale. A catalogue that is 30% the same
  sentence is not the pool it claims to be.

Nothing here is model-specific. The same gates apply to procedurally generated
content, which is how they are tested without a key.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

#: Product text shorter than this is not a description. Two characters is a code
#: fragment or a truncation, never a usable line item.
MIN_LENGTH = 8
#: Longer than this and it will not print on an invoice line without wrapping
#: into the next column. Generous — real SKU names can be long.
MAX_LENGTH = 160

#: Openings that mean the model answered the prompt instead of doing the task.
#: Anchored to the start, case-insensitive; a description legitimately *about*
#: a "sure thing" is not caught.
_PREAMBLE = re.compile(
    r"^\s*(here(?:'s| is| are)\b|sure[,!.]|certainly[,!.]|of course[,!.]|"
    r"okay[,!.]|ok[,!.]|as requested\b|i(?:'ve| have)\b|below (?:is|are)\b|"
    r"product description\s*[:\-]|description\s*[:\-])",
    re.IGNORECASE,
)

#: Markdown, JSON fragments and bullet leaders — the model formatted its answer
#: rather than returning a bare description.
_MARKUP = re.compile(r"(^[\s]*[-*#>]\s|```|^\s*[\[{]|\*\*|__)", re.MULTILINE)

#: Promotional verbs that mark ad copy rather than an invoice line item. An
#: invoice line is a noun phrase ("Ceramic brake pad set, front axle") — it does
#: not "enhance", "ensure" or "deliver" anything. Observed live: an LLM told to
#: "write products" returns "Enhance operations with this energy-efficient …".
#: Matched as a whole word anywhere, case-insensitive; these verbs essentially
#: never appear in a real SKU name, so the false-positive risk is slight and the
#: rejected item just falls back to procedural.
_MARKETING = re.compile(
    r"\b(enhance|ensure|ensures|deliver|delivers|boost|boosts|maximi[sz]e[sd]?|"
    r"experience|discover|introducing|upgrade|transform[s]?|achieve[s]?|unlock[s]?|"
    r"elevate[s]?|streamline[s]?|optimi[sz]e[sd]?|empower[s]?|revolutioni[sz]e[sd]?|"
    r"améliore[rz]?|garantit|assure[rz]?|profitez|découvrez|optimise[rz]?)\b",
    re.IGNORECASE,
)
#: Length past which a line item is almost certainly a sentence. Well above a
#: real SKU name (the procedural ones top out near 70 chars) but below the hard
#: MAX_LENGTH, so it catches prose the promotional-verb list misses.
_PROSE_LENGTH = 90

# ── PII patterns ────────────────────────────────────────────────────────────
# Deliberately broad. A false positive costs one regenerated item; a false
# negative ships personal data in a published file.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
#: Long digit runs: phone numbers, national IDs, card numbers. Product codes are
#: shorter or carry separators, so the floor is set above them.
_LONG_DIGITS = re.compile(r"\b\d[\d\s().-]{8,}\d\b")
#: Real-company tells in text that should be a product, not an entity.
#
# The word boundary goes *before* any trailing period, not after: `Inc\.\b` can
# never match at end of string, because there is no word boundary between "." and
# the end. That silently let "Widget by Acme Inc." through — the exact shape a
# memorised real company would take.
# Known false positive: "SAS" is both a French company suffix and Serial Attached
# SCSI, so "Serial SAS drive bay" is rejected. Kept deliberately — French issuers
# are a first-class part of this corpus, and the cost of a false positive is one
# regenerated item while the cost of a miss is a real company name in a published
# file. Drop SAS from this list if a storage-hardware catalogue ever matters more.
_ENTITY_SUFFIX = re.compile(
    r"\b(?:Inc|LLC|Ltd|PLC|GmbH|SARL|SAS|Pty|Pvt|Corp|Corporation|Holdings|"
    r"Incorporated)\b\.?|\bS\.A\.",
)
#: Personal-name honorifics — a description should not address a person.
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z]\w+")

#: Reserved ranges that are *safe* and must not be flagged: docloom's own
#: synthetic identity uses them deliberately.
_SAFE_DOMAINS = (".example", "example.com", "example.org", "example.net")


@dataclass(frozen=True, slots=True)
class Finding:
    """One rejected item, with the reason it failed."""

    item_id: str
    rule: str
    detail: str
    text: str = ""


@dataclass(slots=True)
class ValidationReport:
    """What a validation pass found. Recorded into the artifact manifest."""

    checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    duplicate_groups: int = 0
    duplicate_items: int = 0

    @property
    def rejected(self) -> int:
        return len({f.item_id for f in self.findings})

    @property
    def ok(self) -> bool:
        return not self.findings and not self.duplicate_items

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.checked if self.checked else 0.0

    def by_rule(self) -> dict[str, int]:
        return dict(Counter(f.rule for f in self.findings))

    def summary(self) -> dict[str, object]:
        """The manifest-facing shape — an artifact ships with its own audit."""
        return {
            "checked": self.checked,
            "rejected": self.rejected,
            "rejection_rate": round(self.rejection_rate, 4),
            "by_rule": self.by_rule(),
            "duplicate_groups": self.duplicate_groups,
            "duplicate_items": self.duplicate_items,
        }


def check_text(item_id: str, text: str) -> list[Finding]:
    """Every quality and PII rule for one description.

    Returns all findings rather than the first, so a regeneration prompt can be
    told everything that was wrong in one pass.
    """
    findings: list[Finding] = []
    if text is None or not text.strip():
        return [Finding(item_id, "empty", "no text", "")]

    stripped = text.strip()
    if len(stripped) < MIN_LENGTH:
        findings.append(Finding(item_id, "too_short", f"{len(stripped)} chars", stripped))
    if len(stripped) > MAX_LENGTH:
        findings.append(Finding(item_id, "too_long", f"{len(stripped)} chars", stripped))
    if _PREAMBLE.search(stripped):
        findings.append(Finding(item_id, "preamble", "assistant preamble", stripped))
    if _MARKUP.search(stripped):
        findings.append(Finding(item_id, "markup", "markdown or structured output", stripped))
    if "\n" in stripped:
        findings.append(Finding(item_id, "multiline", "newline in a line item", stripped))
    if _MARKETING.search(stripped):
        findings.append(Finding(item_id, "marketing", "promotional verb, not a line item",
                                stripped))
    elif len(stripped) > _PROSE_LENGTH and " " in stripped:
        # Long and not obviously promotional, but past what a SKU name runs to —
        # very likely a sentence. `elif` so it is not double-counted with the
        # clearer marketing signal.
        findings.append(Finding(item_id, "prose", f"{len(stripped)} chars, likely a sentence",
                                stripped))

    lowered = stripped.lower()
    for match in _EMAIL.finditer(stripped):
        if not any(safe in match.group().lower() for safe in _SAFE_DOMAINS):
            findings.append(Finding(item_id, "pii_email", match.group(), stripped))
    for match in _URL.finditer(stripped):
        if not any(safe in match.group().lower() for safe in _SAFE_DOMAINS):
            findings.append(Finding(item_id, "pii_url", match.group(), stripped))
    if _LONG_DIGITS.search(stripped):
        findings.append(Finding(item_id, "pii_digits", "long digit run", stripped))
    if _ENTITY_SUFFIX.search(stripped):
        findings.append(Finding(item_id, "entity_name", "company suffix in a product",
                                stripped))
    if _HONORIFIC.search(stripped):
        findings.append(Finding(item_id, "personal_name", "honorific + name", stripped))
    if "lorem ipsum" in lowered:
        findings.append(Finding(item_id, "placeholder", "lorem ipsum", stripped))
    return findings


def _normalise(text: str) -> str:
    """Fold case, accents and whitespace so near-duplicates collide.

    Exact-match dedup misses most LLM repetition: the same description with a
    different article, casing or accent is still the same description as far as a
    corpus is concerned.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def find_duplicates(items: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    """Group item ids by normalised text, returning only the colliding groups."""
    groups: dict[str, list[str]] = {}
    for item_id, text in items:
        groups.setdefault(_normalise(text), []).append(item_id)
    return {k: v for k, v in groups.items() if len(v) > 1}


def validate(
    items: Iterable[tuple[str, str]], *, check_duplicates: bool = True
) -> ValidationReport:
    """Run every gate over ``(item_id, text)`` pairs.

    Duplicates are reported but not attributed to individual items: which member
    of a colliding group is "wrong" is a caller's decision — a per-company
    catalogue may legitimately share a description with another company, while a
    single company's own pool may not.
    """
    report = ValidationReport()
    materialised = list(items)
    report.checked = len(materialised)
    for item_id, text in materialised:
        report.findings.extend(check_text(item_id, text))
    if check_duplicates:
        duplicates = find_duplicates(materialised)
        report.duplicate_groups = len(duplicates)
        report.duplicate_items = sum(len(v) for v in duplicates.values())
    return report
