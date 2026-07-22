"""Run-composition tests — a declared constraint has to actually bind.

The failure these guard against is not a crash. Before this, `run.example.yaml`
let an operator write a slice named ``french`` with ``locales: [fr-FR]``, the
deploy script validated it and printed it back, and the generator ignored it
entirely: 2,500 English invoices, correctly computed, cent-exact, and useless.
Nothing failed. Nothing looked wrong.

So the assertions here are mostly of the form "every document in this slice
really is X", plus the other half of the contract: a constraint that *cannot* be
satisfied raises instead of quietly falling back to an unconstrained draw.
"""

from __future__ import annotations

import pytest

from docloom.core.enums import DocumentCondition
from docloom.core.selection import CRISP_WEAR, Selection, UnsupportedConstraint
from docloom.packs.invoice import InvoicePack
from docloom.packs.invoice.catalog import ALL_ARCHETYPES, GENERAL_ARCHETYPES
from docloom.packs.invoice.sampler import InvoiceSampler


def docs(selection: Selection, n: int = 12, run_id: str = "sel", **kw):  # noqa: ANN201
    sampler = InvoiceSampler(max_line_items=6, selection=selection, **kw)
    return [sampler.generate(run_id, i) for i in range(n)]


# ── Parsing the slice vocabulary ────────────────────────────────────────────
def test_a_bare_scalar_is_accepted_where_a_list_is() -> None:
    assert Selection.from_mapping({"locales": "fr-FR"}).locales == ("fr-FR",)


def test_all_means_unconstrained_not_a_template_named_all() -> None:
    assert Selection.from_mapping({"archetypes": "all"}).archetypes == ()


def test_an_integer_means_use_n_of_them() -> None:
    sel = Selection.from_mapping({"companies": 10, "archetypes": 3})
    assert (sel.company_count, sel.companies) == (10, ())
    assert (sel.archetype_count, sel.archetypes) == (3, ())


def test_sizing_and_identity_keys_are_not_composition() -> None:
    """A slice block carries both; only the composition half belongs here."""
    assert Selection.from_mapping(
        {"name": "french", "pack": "invoice", "count": 2500, "format": "pdf"}
    ).is_empty


def test_condition_singular_and_plural_are_the_same_field() -> None:
    one = Selection.from_mapping({"condition": "handwritten"})
    many = Selection.from_mapping({"conditions": ["handwritten"]})
    assert one.conditions == many.conditions == (DocumentCondition.HANDWRITTEN,)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("crisp", CRISP_WEAR), (0.4, (0.4, 0.4)), ([0.2, 0.8], (0.2, 0.8))],
)
def test_wear_accepts_a_name_a_number_or_a_range(raw: object, expected: tuple) -> None:
    assert Selection.from_mapping({"wear": raw}).wear == expected


@pytest.mark.parametrize(
    "bad",
    [{"wear": [0.9, 0.1]}, {"wear": [0.0, 1.5]}, {"wear": "pristine"},
     {"companies": True}, {"companies": 0}],
)
def test_a_malformed_composition_is_rejected_at_parse_time(bad: dict) -> None:
    with pytest.raises(ValueError):
        Selection.from_mapping(bad)


def test_goods_receipt_cannot_also_be_clean() -> None:
    with pytest.raises(ValueError, match="handwritten delivery note"):
        Selection(goods_receipt=True, conditions=(DocumentCondition.CLEAN,))


# ── The constraints actually bind ───────────────────────────────────────────
def test_a_french_slice_is_french() -> None:
    """The headline regression: this used to produce English invoices."""
    invoices = docs(Selection(locales=("fr-FR", "fr-CA")))
    assert {str(i.locale) for i in invoices} <= {"fr-FR", "fr-CA"}
    assert len(invoices) == 12


def test_locale_carries_currency_and_tax_with_it() -> None:
    """Currency is not a separate knob — it follows the issuer's jurisdiction,
    which is why pinning the locale is how you ask for GBP."""
    assert {str(i.currency) for i in docs(Selection(locales=("en-GB",)))} == {"GBP"}
    assert {str(i.currency) for i in docs(Selection(locales=("fr-FR",)))} == {"EUR"}


def test_a_single_company_slice_uses_exactly_that_company() -> None:
    invoices = docs(Selection(companies=("anchor",)))
    assert {i.company_id for i in invoices} == {"anchor"}


def test_pinning_a_template_overrides_the_companys_own_look() -> None:
    invoices = docs(Selection(companies=("anchor",), archetypes=("boxed-form-01",)))
    assert {i.render_profile.archetype for i in invoices} == {"boxed-form-01"}


def test_use_n_companies_draws_from_exactly_n() -> None:
    invoices = docs(Selection(company_count=3), n=40)
    assert len({i.company_id for i in invoices}) <= 3


def test_the_chosen_subset_is_reproducible_from_the_run_id() -> None:
    """A resumed unit must not draw a different pool and quietly change the
    corpus half way through a run."""
    first = {i.company_id for i in docs(Selection(company_count=4), n=40)}
    second = {i.company_id for i in docs(Selection(company_count=4), n=40)}
    assert first == second


def test_a_different_run_id_may_choose_a_different_subset() -> None:
    a = InvoiceSampler(max_line_items=6, selection=Selection(company_count=3))
    assert a.composition("run-a").roster.companies != a.composition("run-b").roster.companies


def test_a_condition_slice_produces_that_condition() -> None:
    invoices = docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,)))
    assert {i.condition for i in invoices} == {DocumentCondition.HANDWRITTEN}


def test_a_condition_mix_draws_from_the_mix_and_nothing_else() -> None:
    mix = (DocumentCondition.LIGHT_SCAN, DocumentCondition.HEAVY_SCAN)
    assert {i.condition for i in docs(Selection(conditions=mix), n=30)} <= set(mix)


def test_crisp_and_varied_land_on_opposite_sides_of_the_threshold() -> None:
    crisp = docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,), wear=CRISP_WEAR), n=20)
    varied = docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,), wear=(0.35, 1.0)), n=20)
    assert all(i.is_crisp for i in crisp), "a crisp slice must record is_crisp"
    assert all(0.0 <= i.wear <= 0.25 for i in crisp)
    assert not any(i.is_crisp for i in varied)


def test_an_unconstrained_run_is_untouched() -> None:
    """The whole feature is additive: say nothing, get exactly what you got
    before selections existed."""
    invoices = docs(Selection())
    assert {i.condition for i in invoices} == {DocumentCondition.CLEAN}
    assert {i.wear for i in invoices} == {1.0}


def test_business_type_pins_the_industry() -> None:
    assert {str(i.business_type) for i in docs(Selection(business_types=("telecom",)))} == {"telecom"}


def test_a_telecom_slice_is_how_you_ask_for_long_documents() -> None:
    """The one business type that bills hundreds of lines — the answer to
    "one company that generates a lot of distinct line items"."""
    sampler = InvoiceSampler(selection=Selection(business_types=("telecom",), company_count=1))
    invoices = [sampler.generate("tel", i) for i in range(3)]
    assert {i.company_id for i in invoices} == {invoices[0].company_id}   # one issuer
    assert all(len(i.line_items) > 50 for i in invoices)


def test_constraints_compose() -> None:
    invoices = docs(Selection(locales=("fr-FR",), conditions=(DocumentCondition.HANDWRITTEN,)))
    assert {str(i.locale) for i in invoices} == {"fr-FR"}
    assert {i.condition for i in invoices} == {DocumentCondition.HANDWRITTEN}


def test_goods_receipt_still_works_through_the_selection() -> None:
    invoices = docs(Selection(goods_receipt=True), n=6)
    assert all(i.goods_receipt and i.received_date is not None for i in invoices)
    assert {i.condition for i in invoices} == {DocumentCondition.HANDWRITTEN}


def test_the_legacy_goods_receipt_kwarg_folds_into_the_selection() -> None:
    sampler = InvoiceSampler(max_line_items=6, goods_receipt=True)
    assert sampler.selection.goods_receipt is True
    assert sampler.generate("gr", 0).goods_receipt is True


# ── An impossible constraint is loud ────────────────────────────────────────
def test_an_unknown_locale_raises_rather_than_falling_back() -> None:
    with pytest.raises(UnsupportedConstraint, match="no company issues in de-DE"):
        docs(Selection(locales=("de-DE",)), n=1)


def test_an_unknown_company_names_the_ones_that_exist() -> None:
    with pytest.raises(UnsupportedConstraint) as err:
        docs(Selection(companies=("nope",)), n=1)
    assert "no such company: nope" in str(err.value)
    assert "anchor" in str(err.value)


def test_an_unknown_template_raises() -> None:
    with pytest.raises(UnsupportedConstraint, match="no such template"):
        docs(Selection(archetypes=("nope-01",)), n=1)


def test_an_unknown_business_type_raises() -> None:
    with pytest.raises(UnsupportedConstraint):
        docs(Selection(business_types=("underwater-basket-weaving",)), n=1)


def test_asking_for_more_companies_than_match_raises() -> None:
    with pytest.raises(UnsupportedConstraint, match="asked for 999 companies"):
        docs(Selection(company_count=999), n=1)


def test_a_handwritten_slice_cannot_also_pin_a_typeset_template() -> None:
    """The pad *is* the document, so honouring both is impossible; saying so
    beats rendering one and silently ignoring the other."""
    with pytest.raises(UnsupportedConstraint, match="always renders as"):
        docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,),
                       archetypes=("boxed-form-01",)), n=1)


def test_an_impossible_combination_raises_before_any_document() -> None:
    """Resolution happens once, up front — not per document, where a 25,000-doc
    run would fail 25,000 times or, worse, fall back."""
    sampler = InvoiceSampler(selection=Selection(locales=("fr-FR",), business_types=("telecom",),
                                                 company_count=50))
    with pytest.raises(UnsupportedConstraint):
        sampler.composition("x")


# ── The pack seam ───────────────────────────────────────────────────────────
def test_the_pack_passes_a_selection_through_to_its_source() -> None:
    source = InvoicePack().default_source(selection=Selection(locales=("en-GB",)))
    assert {str(source.generate("p", i).locale) for i in range(6)} == {"en-GB"}


def test_max_line_items_reaches_the_sampler() -> None:
    """Regression: `default_source()` took no arguments, so `--max-line-items`
    was accepted by the CLI and silently dropped."""
    source = InvoicePack().default_source(max_line_items=2)
    assert all(len(source.generate("m", i).line_items) <= 2 for i in range(5))


def test_the_archetype_constant_still_matches_the_templates_on_disk() -> None:
    """A renamed template would otherwise only fail when a slice named it."""
    on_disk = {p.name.removesuffix(".html.j2")
               for p in InvoicePack().template_root.glob("archetypes/*.html.j2")}
    assert set(ALL_ARCHETYPES) == on_disk
    assert set(GENERAL_ARCHETYPES) < set(ALL_ARCHETYPES)


def test_a_handwritten_slice_never_draws_a_telecom_issuer() -> None:
    """A pad has ~9-14 ruled lines and the telecom sampler emits 60-400, so a
    telecom issuer here renders a fifty-page 'handwritten' invoice. Found by
    sampling a real run config, not by reading the code."""
    invoices = docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,)), n=40)
    assert "telecom" not in {str(i.business_type) for i in invoices}
    assert all(len(i.line_items) <= 20 for i in invoices)


def test_handwritten_telecom_asked_for_explicitly_is_an_error() -> None:
    """Filtering is right when the operator did not ask for telecom; when they
    did, the two constraints genuinely contradict and silence would be wrong."""
    with pytest.raises(UnsupportedConstraint, match="cannot be an itemised telecom bill"):
        docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,),
                       business_types=("telecom",)), n=1)
