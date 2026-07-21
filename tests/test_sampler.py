"""Invoice sampler + seed catalogue tests.

The load-bearing test is ``test_thousands_of_invoices_all_reconcile``: the sampler
must never produce a record that fails a validator, across thousands of seeds and
every billing model. A single unbalanced golden record would score a correct
extraction as wrong, so "it constructs" *is* the correctness property — the
record's validators do the checking, and this proves they never fire.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal as D

import docloom.packs  # noqa: F401
from docloom.core import get_pack
from docloom.core.money import sum_money
from docloom.core.pipeline import HtmlRenderer, create_run, decode_shard, work_run
from docloom.core.pipeline.source import DocumentSource, stable_seed
from docloom.core.state.sqlite import SqliteStateStore
from docloom.core.storage.local import LocalBlobStore
from docloom.packs.invoice import BusinessType, InvoiceSampler, SeedCatalogue
from docloom.packs.invoice.enums import BillingModel


# ── stable seeding ──────────────────────────────────────────────────────────
def test_stable_seed_is_process_independent() -> None:
    """Regression against using the salted built-in hash(): this value must be
    fixed forever, or reproducibility breaks across workers."""
    assert stable_seed("run_x", 5) == stable_seed("run_x", 5)
    assert stable_seed("run_x", 5) != stable_seed("run_x", 6)
    # A hard-coded expectation pins it against an accidental algorithm change.
    assert stable_seed("run_x", 5) == 10661762636203749462


# ── catalogue ───────────────────────────────────────────────────────────────
def test_roster_is_weighted_toward_the_anchor() -> None:
    roster = SeedCatalogue().roster()
    import random
    rng = random.Random(1)
    counts = Counter(roster.choose(rng).company_id for _ in range(20_000))
    # The anchor's weight is 50000 of ~250000 total → ~20%.
    assert 0.15 < counts["anchor"] / 20_000 < 0.25
    # ...far more than any single "other" company.
    assert counts["anchor"] > counts["co0"] * 5


def test_companies_have_consistent_identity() -> None:
    """A company's look is fixed, so all its invoices read as one vendor."""
    roster = SeedCatalogue().roster()
    anchor = next(c for c in roster.companies if c.company_id == "anchor")
    assert anchor.render_profile.typeface
    assert anchor.render_profile.accent_color.startswith("#")
    assert anchor.party.registrations   # a US company has an EIN


def test_french_companies_use_french_locales() -> None:
    ids = {c.company_id: c for c in SeedCatalogue().roster().companies}
    fr = [c for cid, c in ids.items() if cid.startswith("fr")]
    assert fr
    assert all(c.locale.value.startswith("fr") for c in fr)


# ── sampler determinism & protocol ──────────────────────────────────────────
def test_sampler_satisfies_document_source() -> None:
    assert isinstance(InvoiceSampler(), DocumentSource)


def test_generation_is_deterministic() -> None:
    a, b = InvoiceSampler(), InvoiceSampler()
    assert a.generate("run_x", 42) == b.generate("run_x", 42)


def test_index_is_stamped_through() -> None:
    inv = InvoiceSampler().generate("run_x", 7)
    assert inv.invoice_id == "inv_00000007"
    assert inv.invoice_index == 7
    assert inv.record_id == "inv_00000007"


# ── the correctness property ────────────────────────────────────────────────
def test_thousands_of_invoices_all_reconcile() -> None:
    """Every sampled invoice constructs — meaning every validator (line-sum,
    tax-sum, tier-sum, totals-reconcile) passed. This is the whole point."""
    sampler = InvoiceSampler()
    seen_models: set[str] = set()
    for i in range(3000):
        inv = sampler.generate("run_big", i)                # raises if any validator fails
        # Spot-check the invariants directly too, not just that it built.
        assert sum_money([li.extended_amount for li in inv.line_items]) == inv.totals.subtotal
        assert sum_money([b.amount for b in inv.tax_buckets]) == inv.totals.tax_total
        seen_models.update(inv.billing_models)
    # The 3000 covered a real spread of billing models, not one path.
    assert {"per_unit", "metered_usage", "graduated_tier", "subscription"} <= seen_models


def test_graduated_tier_bands_sum_to_the_line() -> None:
    sampler = InvoiceSampler()
    for i in range(2000):
        inv = sampler.generate("run_tier", i)
        for li in inv.line_items:
            if li.tiers:
                assert sum_money([t.amount for t in li.tiers]) == li.extended_amount


def test_all_locales_and_currencies_appear() -> None:
    sampler = InvoiceSampler()
    locales, currencies = set(), set()
    for i in range(2000):
        inv = sampler.generate("run_var", i)
        locales.add(inv.locale.value)
        currencies.add(inv.currency.value)
    assert {"en-US", "fr-FR", "fr-CA"} <= locales
    assert {"USD", "EUR", "CAD"} <= currencies


def test_french_invoices_carry_french_descriptions() -> None:
    """A French company must not print English product text — even from the
    key-free seed catalogue."""
    sampler = InvoiceSampler(max_line_items=30)
    english_words = ("Stainless", "gasket", "labour", "Bookkeeping", "Voice", "tokens")
    for want in ("fr-FR", "fr-CA"):
        inv = next(i for n in range(3000)
                   if (i := sampler.generate("frd", n)).locale.value == want)
        joined = " ".join(li.description for li in inv.line_items)
        assert not any(w in joined for w in english_words), (want, joined)
        # And it reads as French — accented or French words present.
        assert any(any(ch in li.description for ch in "éèàçûôî") or " de " in li.description
                   for li in inv.line_items)


def test_english_invoices_stay_english() -> None:
    sampler = InvoiceSampler(max_line_items=30)
    inv = next(i for n in range(500)
               if (i := sampler.generate("eng", n)).locale.value == "en-US")
    joined = " ".join(li.description for li in inv.line_items)
    assert not any(fr in joined for fr in ("Jetons", "Requêtes", "Main-d'œuvre"))


def test_quebec_invoices_carry_two_tax_buckets() -> None:
    sampler = InvoiceSampler()
    qc = next(
        inv for i in range(500)
        if (inv := sampler.generate("run_qc", i)).jurisdiction.value == "CA-QC"
    )
    assert {b.code for b in qc.tax_buckets} == {"GST", "QST"}


# ── end to end through the real pipeline ────────────────────────────────────
def test_sampler_drives_a_full_run(tmp_path) -> None:  # noqa: ANN001
    """The sampler is a drop-in DocumentSource: a real run over it produces
    documents and exact golden shards, no keys, no cloud."""
    state = SqliteStateStore(tmp_path / "runs.db")
    blob = LocalBlobStore(tmp_path / "blobs")
    renderer = HtmlRenderer(get_pack("invoice"))
    # Cap line items so a telecom draw doesn't make the test slow.
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=40, unit_size=10)
    stats = work_run(state, run_id="r", source=InvoiceSampler(max_line_items=30),
                     renderer=renderer, blob=blob)

    assert stats.documents_written == 40
    shard = next(blob.iter_keys("r/golden/invoices/"))
    rows = decode_shard(blob.get(shard))
    assert all(isinstance(r["grand_total"], D) for r in rows)
    # Distinct companies appeared (weighted roster), and totals are exact.
    assert len({r["company_id"] for r in rows}) >= 2
