"""Catalogue validation and PII gate tests.

A catalogue is *published* — copied, cached and generated from for months — so a
defect in it is far more expensive than a defect in one run. These gates run
before an artifact is written and their results go into its manifest.

Every rule here exists because of a failure actually observed, not a
hypothetical: empty completions from a reasoning model, assistant preamble, and
the PII classes the whole content design is built to exclude.
"""

from __future__ import annotations

import pytest

from docsynth.packs.invoice.procedural import generate_catalogue
from docsynth.packs.invoice.validation import (
    MAX_LENGTH,
    check_text,
    find_duplicates,
    validate,
)


def rules(text: str) -> set[str]:
    return {f.rule for f in check_text("i", text)}


# ── Good content passes ─────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Stainless steel hex bolt, M8 x 40mm (pack of 10)",
    "Boulon à tête hexagonale en acier inoxydable, M8 x 40 mm (paquet de 10)",
    "Growth plan — monthly subscription",
    "Input tokens (per million)",
    "Ceramic brake pad set, front axle (pair)",
])
def test_realistic_descriptions_pass(text: str) -> None:
    assert rules(text) == set(), text


def test_the_reserved_example_domain_is_not_flagged() -> None:
    """docsynth's own synthetic identity uses `.example` (RFC 2606) deliberately —
    flagging it would reject the safe thing."""
    assert "pii_email" not in rules("Support billing@northwind.example included")
    assert "pii_url" not in rules("Setup guide at www.northwind.example")


# ── Quality gates ───────────────────────────────────────────────────────────
def test_empty_text_is_caught() -> None:
    """The DeepSeek failure: HTTP 200, real usage, no answer. Without this it
    becomes a blank product description in a published file."""
    assert rules("") == {"empty"}
    assert rules("   \n ") == {"empty"}


def test_too_short_and_too_long_are_caught() -> None:
    assert "too_short" in rules("bolt")
    assert "too_long" in rules("x" * (MAX_LENGTH + 1))


@pytest.mark.parametrize("text", [
    "Here is a product description: a widget",
    "Sure! Premium widget for industrial use",
    "Certainly, the item is a steel bracket",
    "Of course! Here you go: a bolt",
    "Product description: heavy duty clamp",
])
def test_assistant_preamble_is_caught(text: str) -> None:
    """The most common way LLM output betrays itself — and it would print on the
    invoice."""
    assert "preamble" in rules(text), text


def test_a_description_merely_about_a_sure_thing_is_not_preamble() -> None:
    """The rule is anchored to the start, so ordinary prose survives."""
    assert "preamble" not in rules("Insurance certificate for sure-grip tooling")


@pytest.mark.parametrize("text", [
    "**Premium** widget", "- Steel bracket", "# Heading widget",
    "```steel bolt```", '{"name": "widget"}',
])
def test_markdown_and_structured_output_are_caught(text: str) -> None:
    assert "markup" in rules(text), text


def test_a_newline_in_a_line_item_is_caught() -> None:
    assert "multiline" in rules("Steel bolt\nwith washer")


def test_placeholder_text_is_caught() -> None:
    assert "placeholder" in rules("Lorem ipsum dolor sit amet widget")


# ── PII gates ───────────────────────────────────────────────────────────────
def test_an_email_is_caught() -> None:
    assert "pii_email" in rules("Contact sales@acme-corp.co.uk for pricing")


def test_a_url_is_caught() -> None:
    assert "pii_url" in rules("See https://realcompany.com/catalogue")


def test_a_phone_number_is_caught() -> None:
    assert "pii_digits" in rules("Call +1 415 555 0199 to order")


def test_a_company_suffix_in_a_product_is_caught() -> None:
    """A description should describe a product, not name an entity — that is how
    a memorised real company would surface."""
    for text in ("Northwind Industrial Ltd", "Widget by Acme Inc.", "Teil GmbH fitting"):
        assert "entity_name" in rules(text), text


def test_a_personal_name_is_caught() -> None:
    assert "personal_name" in rules("Consultation with Dr Okonkwo")


def test_a_product_code_is_not_mistaken_for_a_phone_number() -> None:
    """Short codes and dimensions must survive, or every real SKU is rejected."""
    assert "pii_digits" not in rules("Hex bolt M8 x 40mm, ref HB-4821")
    assert "pii_digits" not in rules("Carton 12x12x8 (bundle of 25)")


# ── Duplicates ──────────────────────────────────────────────────────────────
def test_near_duplicates_collide_across_case_and_whitespace() -> None:
    """Exact matching misses most LLM repetition — the same description with
    different casing is still the same description."""
    groups = find_duplicates([("a", "Steel Bolt"), ("b", "steel   bolt"), ("c", "Nut")])
    assert len(groups) == 1
    assert set(next(iter(groups.values()))) == {"a", "b"}


def test_accents_do_not_hide_a_duplicate() -> None:
    groups = find_duplicates([("a", "Boulon à tête"), ("b", "boulon a tete")])
    assert len(groups) == 1


def test_distinct_text_is_not_grouped() -> None:
    assert find_duplicates([("a", "Steel bolt"), ("b", "Brass washer")]) == {}


# ── The report ──────────────────────────────────────────────────────────────
def test_the_report_counts_and_summarises() -> None:
    report = validate([
        ("a", "Stainless steel hex bolt, M8 x 40mm (pack of 10)"),
        ("b", ""),
        ("c", "Here is a widget description for you"),
    ])
    assert report.checked == 3
    assert report.rejected == 2
    assert not report.ok
    assert report.by_rule() == {"empty": 1, "preamble": 1}
    assert report.summary()["rejection_rate"] == pytest.approx(2 / 3, abs=0.01)


def test_a_clean_batch_reports_ok() -> None:
    report = validate([("a", "Stainless steel hex bolt, M8 x 40mm (pack of 10)")])
    assert report.ok and report.rejected == 0


def test_one_item_failing_several_rules_is_counted_once() -> None:
    """Rejection rate should measure items, not findings, or it can exceed 100%."""
    report = validate([("a", "Here is: **x**\nmore")])
    assert report.rejected == 1
    assert len(report.findings) > 1


# ── Against real generated content ──────────────────────────────────────────
def test_the_procedural_catalogue_passes_its_own_gates() -> None:
    """The gates are not model-specific, so they are proven without a key: if
    procedurally generated content tripped them, they would be wrong."""
    _, products = generate_catalogue(companies=25, products_per_company=40, seed=11)
    report = validate(
        ((f"{cid}:{i}", p.description) for cid, items in products.items()
         for i, p in enumerate(items)),
        check_duplicates=False,
    )
    assert report.rejected == 0, report.by_rule()


def test_french_generated_content_also_passes() -> None:
    _, products = generate_catalogue(companies=25, products_per_company=40, seed=12)
    report = validate(
        ((f"{cid}:{i}", p.fr) for cid, items in products.items()
         for i, p in enumerate(items) if p.fr),
        check_duplicates=False,
    )
    assert report.rejected == 0, report.by_rule()
