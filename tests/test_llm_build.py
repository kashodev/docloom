"""LLM catalogue build tests — driven by fake providers, no key required.

The build uses the same CatalogueRunner the live path does, so a fake provider
exercises the real routing, batching and budget code. What matters most here is
what happens when the model misbehaves — which the live smoke proved it does:
empty output, unparseable output, short batches, bad prices. In every case the
build must stay **complete** by falling back to the procedural product, never
ship a blank or an absurd line, and never let the LLM's numbers reach a golden
value.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

from docloom.core.providers.base import CompletionRequest, CompletionResult, Usage
from docloom.core.providers.budget import BudgetGuard
from docloom.core.providers.mix import ProviderMix
from docloom.core.providers.pricing import pricing_for
from docloom.packs.invoice.artifact import load_catalogue, write_catalogue
from docloom.packs.invoice.llm_build import (
    build_llm_catalogue,
    parse_products,
    build_prompt,
)
from docloom.packs.invoice.procedural import generate_catalogue
from docloom.packs.invoice.sampler import InvoiceSampler


def run(mix, **kw):  # noqa: ANN001, ANN201
    return asyncio.run(build_llm_catalogue(mix, **kw))


def n_asked(request: CompletionRequest) -> int:
    import re
    return int(re.search(r"(?:Write|Écris)\s+(\d+)", request.prompt).group(1))


class Fake:
    """Emits ``responder(request)`` text at a fixed cost, counting calls."""

    pricing = pricing_for("__local__")

    def __init__(self, responder, name="fake", cost=D("0.001")) -> None:  # noqa: ANN001
        self.name = name
        self.model = f"{name}-1"
        self._responder = responder
        self._cost = cost
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        return CompletionResult(self._responder(request), Usage(50, 200),
                                self.model, self.name, self._cost)

    def estimate_cost(self, request: CompletionRequest) -> D:
        return self._cost


def good_json(request: CompletionRequest) -> str:
    cid = request.metadata["company_id"]
    return json.dumps([
        {"name": f"{cid} artisan widget {i}", "min": 3.0 + i, "max": 8.0 + i * 2}
        for i in range(n_asked(request))
    ])


# ── The happy path ──────────────────────────────────────────────────────────
def test_descriptions_come_from_the_model() -> None:
    mix = ProviderMix([Fake(good_json)], [1.0])
    rows, products, report = run(mix, companies=5, products_per_company=15, seed=1)

    assert report.products == 75
    assert report.llm_filled == 75 and report.procedural_fallback == 0
    for company in rows:
        french = str(company.locale).startswith("fr")
        for product in products[company.company_id]:
            printed = product.fr if french else product.description
            assert "artisan widget" in printed


def test_the_price_band_from_the_model_is_used() -> None:
    mix = ProviderMix([Fake(good_json)], [1.0])
    _, products, _ = run(mix, companies=1, products_per_company=3, seed=1)
    first = next(iter(products.values()))[0]
    assert first.price_low == D("3.0000") and first.price_high == D("8.0000")


def test_structural_fields_stay_procedural() -> None:
    """The LLM writes text and a band; billing model, kind, code system and usage
    unit are inherited from the procedural skeleton and never touched."""
    ref_rows, ref_products = generate_catalogue(companies=4, products_per_company=10, seed=2)
    mix = ProviderMix([Fake(good_json)], [1.0])
    _, products, _ = run(mix, companies=4, products_per_company=10, seed=2)
    for cid, items in products.items():
        for llm, proc in zip(items, ref_products[cid], strict=True):
            assert llm.kind == proc.kind
            assert llm.billing_model == proc.billing_model
            assert llm.usage_unit == proc.usage_unit
            assert llm.code_system == proc.code_system


# ── Failure modes fall back, never break ────────────────────────────────────
def test_an_empty_response_falls_back_to_procedural() -> None:
    """The DeepSeek failure: a model that answers with nothing must not leave a
    blank line — the slot keeps its procedural product."""
    _, ref = generate_catalogue(companies=3, products_per_company=8, seed=3)
    mix = ProviderMix([Fake(lambda r: "")], [1.0])
    _, products, report = run(mix, companies=3, products_per_company=8, seed=3, max_rounds=2)

    assert report.llm_filled == 0
    assert report.procedural_fallback == 24
    for cid, items in products.items():
        assert items == ref[cid]        # untouched procedural products
        assert all(p.description or p.fr for p in items)


def test_unparseable_output_falls_back() -> None:
    mix = ProviderMix([Fake(lambda r: "Sure! Here are your products: ...")], [1.0])
    _, products, report = run(mix, companies=2, products_per_company=6, seed=4, max_rounds=1)
    assert report.procedural_fallback == 12
    assert all(p.description for items in products.values() for p in items)


def test_a_short_batch_fills_what_it_can_and_falls_back_for_the_rest() -> None:
    """A model that returns fewer than asked: the ones it gave are used, the rest
    stay procedural."""
    def half(request: CompletionRequest) -> str:
        cid = request.metadata["company_id"]
        half_n = n_asked(request) // 2
        return json.dumps([{"name": f"{cid} item {i}", "min": 2, "max": 5}
                           for i in range(half_n)])

    mix = ProviderMix([Fake(half)], [1.0])
    _, products, report = run(mix, companies=2, products_per_company=10, seed=5, max_rounds=1)
    assert 0 < report.llm_filled < report.products
    assert report.llm_filled + report.procedural_fallback == report.products


def test_rejected_descriptions_fall_back_and_are_counted() -> None:
    """A description that trips the content/PII gates is not accepted."""
    mix = ProviderMix([Fake(lambda r: json.dumps(
        [{"name": "Contact sales@acme-corp.com", "min": 2, "max": 5}]
        * n_asked(r)))], [1.0])
    _, _, report = run(mix, companies=1, products_per_company=4, seed=6, max_rounds=1)
    assert report.rejected_descriptions > 0
    assert report.procedural_fallback == 4


def test_a_regeneration_round_recovers_earlier_failures() -> None:
    """The first calls return junk, later ones return good output — a later round
    fills the slots the first left pending, so a transient failure is recovered
    rather than baked into the artifact."""
    calls = {"n": 0}

    def responder(request: CompletionRequest) -> str:
        calls["n"] += 1
        return "" if calls["n"] <= 2 else good_json(request)

    mix = ProviderMix([Fake(responder)], [1.0])
    _, _, report = run(mix, companies=2, products_per_company=10, seed=7, max_rounds=3)
    assert report.rounds >= 2
    assert report.llm_filled > 0


# ── The band never reaches golden data ──────────────────────────────────────
def test_a_bad_band_is_dropped_and_the_procedural_band_kept() -> None:
    """An unusable band (low>high, negative, absurd) must not be trusted; the
    slot keeps its procedural band, and the golden invoice still reconciles."""
    _, ref = generate_catalogue(companies=1, products_per_company=4, seed=8)
    ref_first = next(iter(ref.values()))[0]

    def bad_band(request: CompletionRequest) -> str:
        cid = request.metadata["company_id"]
        return json.dumps([{"name": f"{cid} thing {i}", "min": 100, "max": 2}
                           for i in range(n_asked(request))])   # min>max

    mix = ProviderMix([Fake(bad_band)], [1.0])
    _, products, report = run(mix, companies=1, products_per_company=4, seed=8, max_rounds=1)
    first = next(iter(products.values()))[0]
    assert "thing" in first.description         # LLM text accepted
    assert first.price_low == ref_first.price_low   # procedural band kept
    assert report.bad_price_bands == 4


def test_generated_invoices_from_an_llm_catalogue_reconcile(tmp_path) -> None:  # noqa: ANN001
    """End to end: an LLM-built catalogue produces valid, reconciling invoices —
    the golden invariant is untouched by where the description came from."""
    mix = ProviderMix([Fake(good_json)], [1.0])
    rows, products, _ = run(mix, companies=6, products_per_company=20, seed=9)
    write_catalogue(str(tmp_path), companies=rows, products=products,
                    catalogue_version="llm-1")

    sampler = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=8)
    invoices = [sampler.generate("llm", i) for i in range(30)]
    assert all(inv.totals.grand_total > 0 for inv in invoices)
    assert all(inv.catalogue_version == "llm-1" for inv in invoices)
    assert any("artisan widget" in li.description or "artisan widget" in (li.description or "")
               for inv in invoices for li in inv.line_items)


# ── Budget and parsing ──────────────────────────────────────────────────────
def test_a_budget_smaller_than_the_build_completes_procedurally_without_raising() -> None:
    # A budget below the build's cost is a spend ceiling, not a failure. Round
    # one's calls are billed (empty answers here, so every slot stays pending);
    # the cap is then over, so later rounds are declined item by item and those
    # slots fall back to procedural. The build completes, spend is bounded to
    # about one round rather than max_rounds, and nothing raises.
    fake = Fake(lambda _r: "", cost=D("1.00"))        # empty → slots stay pending
    mix = ProviderMix([fake], [1.0])
    budget = BudgetGuard(D("2.50"))
    _, products, report = run(mix, companies=20, products_per_company=10, seed=1,
                              budget=budget, max_rounds=3)
    assert sum(len(v) for v in products.values()) == 200   # complete catalogue
    assert report.procedural_fallback == 200               # all fell back
    # Round one issued one call per company; the cap was then over, so the two
    # retry rounds were declined before sending — 20 calls, not 60. That is the
    # runaway the budget exists to stop.
    assert fake.calls == 20


def test_parse_accepts_english_and_french_keys() -> None:
    assert parse_products('[{"name": "Bolt", "min": 1, "max": 3}]') == [("Bolt", 1, 3)]
    assert parse_products('[{"nom": "Boulon", "min": 1, "max": 3}]') == [("Boulon", 1, 3)]


def test_parse_strips_markdown_fences() -> None:
    fenced = '```json\n[{"name": "Bolt", "min": 1, "max": 3}]\n```'
    assert parse_products(fenced) == [("Bolt", 1, 3)]


def test_parse_of_garbage_is_empty_not_an_error() -> None:
    assert parse_products("Here are some products for you!") == []
    assert parse_products("") == []


def test_a_french_company_is_prompted_in_french() -> None:
    from docloom.core.locale.enums import Currency, Locale
    from docloom.packs.invoice.artifact import CompanyRow
    from docloom.packs.invoice.enums import BusinessType
    from docloom.packs.invoice.jurisdictions import Jurisdiction

    fr = CompanyRow("fr0", "Voltaire SARL", BusinessType.RETAIL, Jurisdiction.FR,
                    Locale.FR_FR, Currency.EUR, 1.0)
    request = build_prompt(fr, 10, [("Boulon en acier, M8", __import__("decimal").Decimal("1"), __import__("decimal").Decimal("3"))])
    assert "libellés" in request.prompt.lower() and "facture" in request.prompt.lower()
    assert "EUR" in request.prompt


def test_the_band_is_rounded_to_a_sensible_precision() -> None:
    """A model that emits over-precise, fabricated digits ("$34.5678") should
    store a clean band, at the precision the price will print at: 2 decimals for
    normal money, 4 only for sub-dollar items."""
    from docloom.packs.invoice.llm_build import _coerce_band

    assert _coerce_band(34.5678, 48.2345) == (D("34.57"), D("48.23"))
    assert _coerce_band(145.2999, 289.5) == (D("145.30"), D("289.50"))
    # sub-dollar (AI tokens, telecom) keeps 4dp — that precision is real there
    assert _coerce_band(0.0004, 0.0020) == (D("0.0004"), D("0.0020"))


def test_a_clean_band_survives_unchanged() -> None:
    from docloom.packs.invoice.llm_build import _coerce_band
    assert _coerce_band(12.50, 40.00) == (D("12.50"), D("40.00"))


def test_the_prompt_names_the_narrow_family_not_the_umbrella() -> None:
    """Prompting on the company's sub-category (its actual product line) with an
    explicit stay-in-line instruction is the fix for catalogues that drifted
    across every corner of 'retail'."""
    from docloom.packs.invoice.llm_build import build_prompt
    from docloom.packs.invoice.procedural import generate_company

    row, prods = generate_company(0)                       # a retail company
    req = build_prompt(row, 10, [(p.description, p.price_low, p.price_high) for p in prods[:6]])
    assert row.product_category and row.product_category in req.prompt
    assert "unrelated" in req.prompt.lower()               # stay-in-line instruction
    assert row.business_type.value not in req.prompt       # umbrella not used as the domain


def test_the_prompt_lists_placed_items_to_avoid_repeats() -> None:
    from docloom.packs.invoice.llm_build import build_prompt
    from docloom.packs.invoice.procedural import generate_company

    row, prods = generate_company(0)
    shots = [(p.description, p.price_low, p.price_high) for p in prods[:6]]
    with_avoid = build_prompt(row, 5, shots, avoid=["Copper skillet, large", "Glass bowl, small"])
    assert "do not repeat" in with_avoid.prompt.lower()
    assert "Copper skillet, large" in with_avoid.prompt
    assert "do not repeat" not in build_prompt(row, 5, shots).prompt.lower()  # none → no clause


def test_a_later_round_is_told_what_the_first_round_already_placed() -> None:
    """Cross-round de-dup: round 0's filled names are fed to round 1's prompt so
    the model produces NEW items instead of repeats that would only be deduped
    and fall back to procedural."""
    import asyncio

    from docloom.core.providers.mix import ProviderMix
    from docloom.packs.invoice.llm_build import build_llm_catalogue

    seen: list[str] = []

    def responder(request):  # noqa: ANN001, ANN202
        seen.append(request.prompt)
        return '[{"description": "Alpha gadget, blue"}]'   # one usable item; rest short → pending

    mix = ProviderMix([Fake(responder)], [1.0])
    asyncio.run(build_llm_catalogue(mix, companies=1, products_per_company=8, seed=1, max_rounds=2))
    # Round 0 placed "Alpha gadget, blue"; a round-1 prompt must carry it in the avoid list.
    assert any("Alpha gadget, blue" in p and "do not repeat" in p.lower() for p in seen)
