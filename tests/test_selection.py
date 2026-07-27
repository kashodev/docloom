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

from datetime import date

import pytest

from docloom.core.enums import DocumentCondition
from docloom.core.selection import CRISP_WEAR, Selection, UnsupportedConstraint
from docloom.packs.invoice import InvoicePack
from docloom.packs.invoice.catalog import ALL_ARCHETYPES, GENERAL_ARCHETYPES
from docloom.packs.invoice.sampler import InvoiceSampler


def docs(selection: Selection, n: int = 12, run_id: str = "sel", **kw):
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


def test_a_handwritten_slice_never_draws_a_born_digital_issuer() -> None:
    """A software or AI-platform business is too new to have hand-filled an invoice
    on a pad, so a handwritten slice must never draw one."""
    kinds = {str(i.business_type)
             for i in docs(Selection(conditions=(DocumentCondition.HANDWRITTEN,)), n=40)}
    assert "b2b_saas" not in kinds and "ai_platform" not in kinds, kinds


def test_a_scanned_slice_never_draws_a_born_digital_issuer() -> None:
    """Born-digital issuers have no paper original old enough to survive as a
    degraded scan, so light and heavy scans exclude them too."""
    for cond in (DocumentCondition.LIGHT_SCAN, DocumentCondition.HEAVY_SCAN):
        kinds = {str(i.business_type) for i in docs(Selection(conditions=(cond,)), n=40)}
        assert "b2b_saas" not in kinds and "ai_platform" not in kinds, (cond, kinds)


def test_a_clean_slice_still_allows_a_born_digital_issuer() -> None:
    """The exclusion is scoped to non-CLEAN conditions — a clean digital PDF from a
    SaaS company is exactly right, so the filter must leave it alone."""
    invoices = docs(Selection(business_types=("b2b_saas",)), n=8)  # default is CLEAN
    assert {str(i.business_type) for i in invoices} == {"b2b_saas"}


def test_born_digital_asked_for_explicitly_with_a_scan_is_an_error() -> None:
    """Filtering is right when the operator did not name a born-digital type; when
    they pinned one against a scan, the two constraints genuinely contradict."""
    with pytest.raises(UnsupportedConstraint, match="born-digital"):
        docs(Selection(conditions=(DocumentCondition.HEAVY_SCAN,),
                       business_types=("b2b_saas",)), n=1)


# ── Issue-date range ────────────────────────────────────────────────────────
def test_a_date_range_parses_to_an_issue_window() -> None:
    sel = Selection.from_mapping({"date_range": ["2023-01-01", "2023-06-30"]})
    assert sel.issue_date_range == (date(2023, 1, 1), date(2023, 6, 30))


def test_issue_dates_is_an_accepted_alias_and_takes_a_mapping() -> None:
    sel = Selection.from_mapping({"issue_dates": {"from": "2023-01-01", "to": "2023-02-01"}})
    assert sel.issue_date_range == (date(2023, 1, 1), date(2023, 2, 1))


def test_a_backwards_date_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="from, to"):
        Selection.from_mapping({"date_range": ["2023-12-31", "2023-01-01"]})


def test_a_single_date_is_not_a_range() -> None:
    with pytest.raises(ValueError, match="from, to"):
        Selection.from_mapping({"date_range": "2023-01-01"})


def test_a_malformed_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid date"):
        Selection.from_mapping({"date_range": ["2023-13-01", "2023-12-31"]})


def test_describe_includes_the_date_window() -> None:
    sel = Selection(issue_date_range=(date(2023, 1, 1), date(2023, 6, 30)))
    assert "dates=2023-01-01..2023-06-30" in sel.describe()


def test_issue_dates_fall_within_the_configured_range() -> None:
    """Each invoice's issue date is drawn uniformly from the range — inside it, and
    actually varying across the range rather than pinned to one day."""
    lo, hi = date(2021, 3, 1), date(2021, 3, 31)
    issued = [i.issue_date for i in docs(Selection(issue_date_range=(lo, hi)), n=40)]
    assert all(lo <= d <= hi for d in issued), issued
    assert len(set(issued)) > 1


def test_other_dates_stay_logically_bound_to_the_issue_date() -> None:
    """Due date and payment-received never precede issue; a billing period ends on
    or before issue (the invoice is issued for a period it already covers)."""
    lo, hi = date(2021, 1, 1), date(2021, 12, 31)
    for inv in docs(Selection(issue_date_range=(lo, hi)), n=40):
        assert inv.due_date is None or inv.due_date >= inv.issue_date
        assert inv.received_date is None or inv.received_date >= inv.issue_date
        for li in inv.line_items:
            if li.period_end is not None:
                assert li.period_start <= li.period_end <= inv.issue_date, inv.invoice_id


def test_a_subscription_billing_period_is_bound_to_the_issue_date() -> None:
    """A b2b_saas issuer bills subscription lines with an explicit period; it must
    fall on or before the (ranged) issue date, not float free of it."""
    lo, hi = date(2021, 1, 1), date(2021, 1, 31)
    invoices = docs(Selection(business_types=("b2b_saas",), issue_date_range=(lo, hi)), n=20)
    periods = [(li.period_start, li.period_end, inv.issue_date)
               for inv in invoices for li in inv.line_items if li.period_end is not None]
    assert periods, "expected subscription lines carrying billing periods"
    assert all(ps <= pe <= issue for ps, pe, issue in periods)


def test_the_default_issue_window_is_unchanged_when_unset() -> None:
    """No range configured keeps the pack's prior default window (2026), so an
    unconfigured run behaves exactly as it did before the knob existed."""
    issued = [i.issue_date for i in docs(Selection(), n=40)]
    assert all(date(2026, 1, 1) <= d <= date(2026, 11, 27) for d in issued), issued


# ── Business-type era floor ─────────────────────────────────────────────────
def test_ai_platform_issue_dates_are_floored_to_its_era() -> None:
    """An AI-platform vendor is a very recent business: even inside a window that
    reaches back to 2023, its issue date never precedes its 2025 era."""
    invoices = docs(Selection(business_types=("ai_platform",),
                              issue_date_range=(date(2023, 1, 1), date(2025, 12, 31))), n=40)
    assert invoices, "expected ai_platform invoices in the seed roster"
    assert all(i.issue_date >= date(2025, 1, 1) for i in invoices), \
        min(i.issue_date for i in invoices)


def test_the_era_floor_is_a_soft_default_that_can_be_switched_off() -> None:
    """Realism here is opt-out, never a hard stop: with the floor off, an AI-platform
    issuer is allowed a deliberately anachronistic pre-era date, and the run is not
    blocked."""
    lo, hi = date(2019, 1, 1), date(2021, 12, 31)
    invoices = docs(Selection(business_types=("ai_platform",),
                              issue_date_range=(lo, hi), enforce_date_era=False), n=40)
    assert invoices, "a pre-era window must still produce AI invoices, not raise"
    assert all(lo <= i.issue_date <= hi for i in invoices)
    assert any(i.issue_date < date(2025, 1, 1) for i in invoices)   # actually anachronistic


def test_enforce_date_era_defaults_on_and_parses_off() -> None:
    assert Selection.from_mapping({}).enforce_date_era is True
    assert Selection.from_mapping({"enforce_date_era": False}).enforce_date_era is False


def test_a_non_born_digital_type_ignores_the_era_floor() -> None:
    """Retail has always issued invoices, so an older window is honoured as-is."""
    lo, hi = date(2019, 1, 1), date(2021, 12, 31)
    invoices = docs(Selection(business_types=("retail",), issue_date_range=(lo, hi)), n=30)
    assert all(lo <= i.issue_date <= hi for i in invoices), invoices
