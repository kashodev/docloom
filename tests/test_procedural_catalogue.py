"""Procedural catalogue tests — a large content pool for nothing.

Step 4 of the catalogue plan. Its job is to prove the artifact pipeline end to
end at zero cost, and to deliver most of the realism before any LLM spend, so the
expensive step is an enhancement rather than a dependency.

The properties worth pinning: a company's own catalogue never repeats a SKU, the
build is deterministic, every locale is represented, and French output is
*wholly* French — a half-translated string is text no extractor would ever meet.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from random import Random

import pytest

from docloom.packs.invoice.artifact import load_catalogue, write_catalogue
from docloom.packs.invoice.procedural import (
    BUSINESS_TYPES,
    combination_space,
    company_name_space,
    generate_catalogue,
    generate_products,
    untranslated_slots,
)
from docloom.packs.invoice.sampler import InvoiceSampler


# ── The pool itself ─────────────────────────────────────────────────────────
def test_a_companys_own_catalogue_never_repeats_a_sku() -> None:
    """Distinct *within a company* is the property that matters — a vendor's
    catalogue listing the same SKU twice is simply wrong."""
    for business_type in BUSINESS_TYPES:
        products = generate_products(Random(0), business_type, 120)
        assert len({p.description for p in products}) == 120, business_type


def test_asking_for_more_than_the_slots_express_raises() -> None:
    """Silently returning duplicates would put the defect into a published
    artifact, where it is expensive to notice."""
    smallest = min(BUSINESS_TYPES, key=combination_space)
    with pytest.raises(ValueError, match="distinct products"):
        generate_products(Random(0), smallest, combination_space(smallest) + 1)


def test_every_business_type_can_fill_a_realistic_company_catalogue() -> None:
    """300 SKUs per company is the sizing the 1M-invoice design calls for, so
    every type must be able to express at least that many."""
    for business_type in BUSINESS_TYPES:
        assert combination_space(business_type) >= 300, business_type


def test_the_combinatorial_space_is_large_but_finite() -> None:
    """Recorded deliberately: combinatorics have a ceiling, and that ceiling is
    the argument for the LLM step. ~8k distinct descriptions is 300x the seed
    catalogue's 25, and still far short of a 300k pool."""
    total = sum(combination_space(b) for b in BUSINESS_TYPES)
    assert total > 5_000
    assert company_name_space() > 500


# ── Determinism ─────────────────────────────────────────────────────────────
def test_the_build_is_deterministic() -> None:
    """A published artifact must be rebuildable and verifiable, not trusted."""
    a_rows, a_products = generate_catalogue(companies=8, products_per_company=15, seed=7)
    b_rows, b_products = generate_catalogue(companies=8, products_per_company=15, seed=7)
    assert [r.name for r in a_rows] == [r.name for r in b_rows]
    assert ([p.description for v in a_products.values() for p in v]
            == [p.description for v in b_products.values() for p in v])


def test_a_different_seed_gives_different_content() -> None:
    a, _ = generate_catalogue(companies=8, products_per_company=10, seed=1)
    b, _ = generate_catalogue(companies=8, products_per_company=10, seed=2)
    assert [r.name for r in a] != [r.name for r in b]


# ── Composition ─────────────────────────────────────────────────────────────
def test_every_locale_is_represented() -> None:
    """A corpus that is 95% en-US cannot exercise a French extractor."""
    rows, _ = generate_catalogue(companies=30, products_per_company=5, seed=0)
    assert {str(r.locale) for r in rows} >= {"en-US", "en-GB", "fr-FR", "fr-CA"}


def test_currency_follows_jurisdiction() -> None:
    rows, _ = generate_catalogue(companies=30, products_per_company=5, seed=0)
    by_locale = {str(r.locale): str(r.currency) for r in rows}
    assert by_locale["en-GB"] == "GBP"
    assert by_locale["fr-FR"] == "EUR"
    assert by_locale["en-US"] == "USD"


def test_weights_form_a_long_tail() -> None:
    """Most companies issue a little, a few issue a lot — what a real corpus
    looks like, and what makes some vendors dominate the sample."""
    rows, _ = generate_catalogue(companies=200, products_per_company=5, seed=0)
    weights = sorted((r.weight for r in rows), reverse=True)
    assert weights[0] > weights[len(weights) // 2] * 3


def test_prices_are_exact_decimals() -> None:
    for business_type in BUSINESS_TYPES:
        for product in generate_products(Random(0), business_type, 20):
            assert isinstance(product.price_low, Decimal)
            assert product.price_high > product.price_low > 0


# ── French output is wholly French ──────────────────────────────────────────
def test_no_slot_value_is_left_untranslated() -> None:
    """Guards against an added English value nobody translated. A mixed-language
    string like "Consultation-conseil, expedited (engagement)" is worse than
    either language: it is not text a French extractor would ever meet, so it
    tests nothing and looks broken."""
    assert untranslated_slots() == {}


def test_a_french_company_gets_wholly_french_descriptions(tmp_path: Path) -> None:
    rows, products = generate_catalogue(companies=12, products_per_company=10, seed=5)
    french = [r for r in rows if str(r.locale).startswith("fr")]
    assert french
    english_tells = (" of ", "each", "pack", "per ", "monthly", "standard fit")
    for row in french:
        for product in products[row.company_id]:
            assert product.fr, "a French company's products need French text"
            assert not any(t in product.fr for t in english_tells), product.fr


# ── Through the artifact, end to end ────────────────────────────────────────
def test_a_generated_catalogue_round_trips_through_an_artifact(tmp_path: Path) -> None:
    """The whole point of step 4: prove the artifact pipeline with no key and no
    spend before the LLM build depends on it."""
    rows, products = generate_catalogue(companies=10, products_per_company=25, seed=2)
    write_catalogue(str(tmp_path), companies=rows, products=products,
                    catalogue_version="procedural-1",
                    provenance={"generator": "procedural", "seed": 2})

    catalogue = load_catalogue(str(tmp_path))
    assert catalogue.version == "procedural-1"
    assert len(catalogue.roster()) == 10
    for company in catalogue.roster().companies:
        assert len(catalogue.spec_for(company).products) == 25


def test_generating_invoices_from_a_procedural_catalogue(tmp_path: Path) -> None:
    rows, products = generate_catalogue(companies=8, products_per_company=40, seed=4)
    write_catalogue(str(tmp_path), companies=rows, products=products,
                    catalogue_version="procedural-1")
    sampler = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=10)

    invoices = [sampler.generate("proc", i) for i in range(40)]
    assert all(inv.totals.grand_total > 0 for inv in invoices)
    assert all(inv.catalogue_version == "procedural-1" for inv in invoices)

    descriptions = {li.description for inv in invoices for li in inv.line_items}
    # Anchored to something meaningful rather than a round number: 40 invoices
    # should yield more distinct descriptions than the *entire* seed catalogue
    # contains (25 across all business types). Weighted company selection means a
    # few issuers dominate, so this is well short of 8 x 40 and deliberately so.
    assert len(descriptions) > 2 * 25, len(descriptions)


def test_a_procedural_corpus_is_far_more_varied_than_the_seed_pool(tmp_path: Path) -> None:
    """Quantifies the improvement rather than asserting a vague 'more'."""
    rows, products = generate_catalogue(companies=20, products_per_company=100, seed=6)
    write_catalogue(str(tmp_path), companies=rows, products=products,
                    catalogue_version="p1")
    artifact = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=8)
    seed_pool = InvoiceSampler(max_line_items=8)

    def distinct(sampler) -> int:  # noqa: ANN001
        return len({li.description
                    for i in range(120)
                    for li in sampler.generate("cmp", i).line_items})

    from_artifact, from_seed = distinct(artifact), distinct(seed_pool)
    assert from_artifact > from_seed * 10, (from_artifact, from_seed)
