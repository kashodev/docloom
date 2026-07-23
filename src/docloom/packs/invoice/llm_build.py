"""LLM-backed catalogue generation.

The procedural builder expresses ~7,900 distinct descriptions from hand-authored
slots — a ceiling. This one lifts it: an LLM writes the product descriptions, so
the pool gains the semantic long tail combinatorics cannot reach, while
everything else stays exactly as the procedural build left it.

What the LLM does and does not touch is deliberate:

* **It writes descriptions and suggests a price band.** Co-generated, so the two
  are coherent — the model prices a tractor as a tractor, which is what stops the
  "$2 tractor" a per-business-type band would produce. See
  ``feature_explorations/llm-catalogue-and-pii.md`` §2.
* **It never writes a golden number.** The band is a *sampling input*; the sampler
  still draws the price, computes quantity × price, sums and taxes. A bad band
  produces an implausible-but-reconciling invoice, never broken golden data. So
  the band is validated (positive, low<high, sane ratio and envelope) but not
  trusted for correctness.
* **Everything structural stays procedural** — business type, billing model,
  code system, usage unit, and the company roster — because those must be
  arithmetically and semantically sane, and the LLM adds nothing there.

The build reuses the procedural catalogue as its **fallback and its skeleton**:
the roster comes from it, the billing metadata comes from it, and any slot the
LLM cannot fill (a failed call, an unparseable line, a rejected description)
keeps its procedural product. No company is ever left with an empty catalogue,
and a total provider outage degrades to the procedural build rather than failing.

Nothing here calls a model directly. It drives :class:`CatalogueRunner`, so
routing, the budget guard, the batch path and usage recording are the same code
the whole provider stack is built on — and the same code the tests exercise with
fake providers, no key required.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from docloom.core.locale.enums import Locale
from docloom.core.providers.budget import BudgetGuard
from docloom.core.providers.catalogue_runner import CatalogueItem, CatalogueRunner
from docloom.core.providers.base import CompletionRequest
from docloom.core.providers.mix import ProviderMix
from docloom.packs.invoice.artifact import CompanyRow
from docloom.packs.invoice.catalog import ProductTemplate
from docloom.packs.invoice.procedural import generate_catalogue
from docloom.packs.invoice.validation import check_text

#: Products asked for in one call. The batch amortises the input tokens (the
#: per-company context is repeated once per chunk, not once per product) and
#: keeps a 300k-item build to ~6k calls rather than 300k.
CHUNK = 50

#: A single SKU's price range should not span orders of magnitude.
_MAX_RATIO = Decimal("6")

#: Broad per-nothing sanity envelope: catches a $0 or a $2M line, not a $2
#: tractor (that is the co-generation's job, §2). Applied to every band
#: regardless of business type — the point is only to reject the absurd.
_MIN_PRICE, _MAX_PRICE = Decimal("0.0001"), Decimal("100000")


@dataclass(slots=True)
class BuildReport:
    """What an LLM build produced, for the artifact's provenance block."""

    companies: int = 0
    products: int = 0
    llm_filled: int = 0
    procedural_fallback: int = 0
    rounds: int = 0
    rejected_descriptions: int = 0
    bad_price_bands: int = 0
    total_cost: Decimal = Decimal(0)
    by_provider: dict[str, int] = field(default_factory=dict)

    @property
    def llm_fraction(self) -> float:
        return self.llm_filled / self.products if self.products else 0.0

    def summary(self) -> dict[str, object]:
        return {
            "generator": "llm",
            "companies": self.companies,
            "products": self.products,
            "llm_filled": self.llm_filled,
            "procedural_fallback": self.procedural_fallback,
            "llm_fraction": round(self.llm_fraction, 4),
            "rounds": self.rounds,
            "rejected_descriptions": self.rejected_descriptions,
            "bad_price_bands": self.bad_price_bands,
            "total_cost_usd": str(self.total_cost),
            "by_provider": dict(self.by_provider),
        }


def _is_french(locale: Locale) -> bool:
    return str(locale).startswith("fr")


def _few_shot(examples: list[tuple[str, Decimal, Decimal]]) -> str:
    """The example products as the JSON the model should return, so the style and
    the format are shown together — the most reliable way to pin both."""
    return json.dumps(
        [{"name": text, "min": float(lo), "max": float(hi)} for text, lo, hi in examples],
        ensure_ascii=False,
    )


def build_prompt(
    company: CompanyRow, count: int, examples: list[tuple[str, Decimal, Decimal]]
) -> CompletionRequest:
    """One chunk request: ``count`` line items for one company, in its language.

    The register is the thing to get right: an invoice line item is a terse
    *noun phrase* with a spec — "Stainless steel hex bolt, M8 x 40mm (pack of
    10)" — not a marketing sentence ("Enhance operations with our energy-efficient
    …"). A model told only "write products" reaches for ad copy, so the prompt is
    shown the procedural descriptions as few-shot examples and told to match them
    exactly. Anchoring to real examples fixes the format, the domain and the
    register at once, far more reliably than an adjective like "terse".

    French companies are prompted in French with French examples, because that is
    what prints on their invoices — translating afterwards is what produced the
    half-French strings the procedural build had to fix.
    """
    french = _is_french(company.locale)
    currency = str(company.currency)
    kind = company.business_type.value.replace("_", " ")
    shots = _few_shot(examples)
    if french:
        system = (
            "Tu génères des libellés de lignes de facture : un nom de produit ou "
            "de service concis, tel qu'il apparaît sur une vraie facture — un "
            "groupe nominal avec ses caractéristiques, jamais une phrase, jamais "
            "un texte publicitaire, sans verbe promotionnel. Réponds uniquement "
            "par un tableau JSON."
        )
        prompt = (
            f"Entreprise : {company.name}, secteur « {kind} ».\n"
            f"Écris {count} libellés de lignes de facture distincts, chacun avec "
            f"une fourchette de prix plausible en {currency}.\n"
            f"Reproduis exactement le style et le format de ces exemples :\n{shots}\n"
            'Chaque « name » est une seule ligne : produit/service + '
            "caractéristique clé, sans phrase ni argumentaire."
        )
    else:
        system = (
            "You write invoice line-item labels: a terse product or service name "
            "as it appears on a real invoice — a noun phrase with its spec, never "
            "a sentence, never marketing copy, no promotional verbs like 'enhance' "
            "or 'deliver'. Reply with a JSON array only."
        )
        prompt = (
            f"Company: {company.name}, a {kind} business.\n"
            f"Write {count} distinct invoice line items, each with a plausible "
            f"price range in {currency}.\n"
            f"Match the style and format of these examples exactly:\n{shots}\n"
            "Each 'name' is one short line: product/service plus a key spec — no "
            "sentence, no marketing."
        )
    return CompletionRequest(system=system, prompt=prompt, max_tokens=40 * count,
                             metadata={"company_id": company.company_id})


def _coerce_band(low: object, high: object) -> tuple[Decimal, Decimal] | None:
    """A validated (low, high) band, or None if it is unusable.

    The band is a generation hint, so a bad one is dropped in favour of the
    procedural fallback rather than trusted — it can never reach a golden value.
    """
    try:
        lo, hi = Decimal(str(low)), Decimal(str(high))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if lo <= 0 or hi <= 0 or hi < lo:
        return None
    if lo < _MIN_PRICE or hi > _MAX_PRICE:
        return None
    if lo > 0 and hi / lo > _MAX_RATIO:
        return None
    # Round to the precision the price will actually print at, so the stored band
    # is clean rather than carrying the model's fabricated extra digits (observed:
    # "$34.5678"). Two decimals for normal money, four only for sub-dollar items
    # (AI tokens, telecom) — the same rule the sampler uses when it draws a price.
    places = Decimal("0.0001") if hi < Decimal("1") else Decimal("0.01")
    return lo.quantize(places), hi.quantize(places)


def parse_products(text: str) -> list[tuple[str, object, object]]:
    """Pull ``(name, min, max)`` triples out of a model response.

    Tolerant: strips markdown fences, accepts the English or French key names,
    and skips anything malformed rather than failing the whole chunk — a
    dropped item just falls back to procedural.
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[tuple[str, object, object]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("nom") or entry.get("description")
        low = entry.get("min", entry.get("low"))
        high = entry.get("max", entry.get("high"))
        if isinstance(name, str) and name.strip():
            out.append((name.strip(), low, high))
    return out


def _overlay(fallback: ProductTemplate, name: str, band: tuple[Decimal, Decimal] | None,
             french: bool) -> ProductTemplate:
    """A procedural product with the LLM's text (and band, if usable) swapped in.

    Inheriting the fallback keeps the structural fields — kind, billing model,
    code system, usage unit — correct for the business type, which the LLM never
    touches. The band replaces the procedural one only when valid.
    """
    updates: dict[str, object] = {"fr": name} if french else {"description": name}
    if band is not None:
        updates["price_low"], updates["price_high"] = band
    return replace(fallback, **updates)


def _printed_text(product: ProductTemplate, french: bool) -> str:
    return product.fr if french else product.description


async def build_llm_catalogue(
    mix: ProviderMix,
    *,
    companies: int = 1_000,
    products_per_company: int = 300,
    seed: int = 0,
    budget: BudgetGuard | None = None,
    max_rounds: int = 3,
    concurrency: int = 8,
    use_batch: bool = True,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[CompanyRow], dict[str, list[ProductTemplate]], BuildReport]:
    """Build a catalogue whose descriptions come from ``mix``.

    ``progress`` is called with a human line at each round boundary, so a
    long build (thousands of calls, minutes) is not silent. It is a callback
    rather than a print so the caller decides where it goes — the CLI sends it to
    the terminal; a future structured logger could take it instead.

    Returns the same ``(rows, products)`` shape as
    :func:`~docloom.packs.invoice.procedural.generate_catalogue`, so it drops
    straight into :func:`~docloom.packs.invoice.artifact.write_catalogue`, plus a
    :class:`BuildReport` for the manifest provenance.

    Structure: the roster and a full procedural product set are generated first
    as the skeleton and fallback; then each company's descriptions are requested
    in chunks, validated, and overlaid onto that skeleton. Slots the LLM cannot
    fill after ``max_rounds`` keep their procedural product, so the result is
    always complete and a provider outage degrades to the procedural build.
    """
    rows, fallback = generate_catalogue(
        companies=companies, products_per_company=products_per_company, seed=seed
    )
    by_id = {r.company_id: r for r in rows}
    products: dict[str, list[ProductTemplate]] = {
        cid: list(items) for cid, items in fallback.items()
    }
    # Two procedural products per company become the prompt's few-shot examples —
    # the style the LLM must match. Taken from the fallback, in the company's
    # printed language, with their price bands so the format is shown too.
    examples: dict[str, list[tuple[str, Decimal, Decimal]]] = {
        cid: [
            (_printed_text(p, _is_french(by_id[cid].locale)), p.price_low, p.price_high)
            for p in items[:2]
        ]
        for cid, items in fallback.items()
    }
    report = BuildReport(companies=len(rows),
                         products=sum(len(v) for v in products.values()))

    # Slots still needing an LLM description: (company_id, index) into products.
    pending: list[tuple[str, int]] = [
        (cid, i) for cid, items in products.items() for i in range(len(items))
    ]
    report.procedural_fallback = len(pending)   # updated as slots fill

    runner = CatalogueRunner(mix, budget=budget, concurrency=concurrency,
                             use_batch=use_batch)
    emit = progress or (lambda _msg: None)
    emit(f"skeleton ready: {report.companies:,} companies, {report.products:,} products; "
         f"requesting descriptions in chunks of {CHUNK}")

    for round_no in range(max_rounds):
        if not pending:
            break
        report.rounds = round_no + 1
        items, slot_map = _chunk_items(pending, by_id, examples, round_no)
        emit(f"round {round_no + 1}: {len(items):,} chunk(s) for {len(pending):,} "
             f"pending slot(s)…")
        outcome = await runner.run(items)
        report.total_cost += outcome.total_cost
        for provider, n in outcome.by_provider.items():
            report.by_provider[provider] = report.by_provider.get(provider, 0) + n

        still_pending: list[tuple[str, int]] = []
        # Slots whose chunk failed outright are retried next round untouched.
        failed_chunks = set(outcome.failures)
        for item_id, slots in slot_map.items():
            if item_id in failed_chunks:
                still_pending.extend(slots)
                continue
            result = outcome.results.get(item_id)
            parsed = parse_products(result.text) if result else []
            still_pending.extend(_apply_chunk(parsed, slots, by_id, products, report))
        pending = still_pending
        filled = report.products - len(pending)
        emit(f"round {round_no + 1} done: {filled:,}/{report.products:,} filled "
             f"({filled / report.products:.1%}), {len(pending):,} still pending, "
             f"cost ${report.total_cost:.4f}")

    report.procedural_fallback = len(pending)
    report.llm_filled = report.products - report.procedural_fallback
    return rows, products, report


def _chunk_items(
    pending: list[tuple[str, int]],
    by_id: dict[str, CompanyRow],
    examples: dict[str, list[tuple[str, Decimal, Decimal]]],
    round_no: int,
) -> tuple[list[CatalogueItem], dict[str, list[tuple[str, int]]]]:
    """Group pending slots into per-company chunk requests.

    Item ids embed the round so a regeneration routes independently of the first
    attempt — a chunk that a weak model fumbled can land on a different one.
    """
    by_company: dict[str, list[tuple[str, int]]] = {}
    for cid, i in pending:
        by_company.setdefault(cid, []).append((cid, i))

    items: list[CatalogueItem] = []
    slot_map: dict[str, list[tuple[str, int]]] = {}
    for cid, slots in by_company.items():
        for offset in range(0, len(slots), CHUNK):
            chunk = slots[offset:offset + CHUNK]
            item_id = f"{cid}:r{round_no}:{offset // CHUNK}"
            items.append(CatalogueItem(
                item_id, build_prompt(by_id[cid], len(chunk), examples[cid])))
            slot_map[item_id] = chunk
    return items, slot_map


def _apply_chunk(
    parsed: list[tuple[str, object, object]],
    slots: list[tuple[str, int]],
    by_id: dict[str, CompanyRow],
    products: dict[str, list[ProductTemplate]],
    report: BuildReport,
) -> list[tuple[str, int]]:
    """Overlay parsed products onto their slots; return the slots still unfilled.

    A description that fails the content/PII gates, or that duplicates one
    already placed for this company, is not accepted — the slot stays pending for
    the next round and keeps its procedural product meanwhile.
    """
    unfilled: list[tuple[str, int]] = []
    company = by_id[slots[0][0]]
    french = _is_french(company.locale)
    placed = {_printed_text(p, french).strip().lower()
              for p in products[company.company_id]}

    for slot, triple in zip(slots, parsed, strict=False):
        cid, index = slot
        name, low, high = triple
        if check_text(f"{cid}:{index}", name):
            report.rejected_descriptions += 1
            unfilled.append(slot)
            continue
        if name.strip().lower() in placed:
            unfilled.append(slot)          # duplicate within the company
            continue
        band = _coerce_band(low, high)
        if band is None:
            report.bad_price_bands += 1     # kept: fall back to the procedural band
        products[cid][index] = _overlay(products[cid][index], name, band, french)
        placed.add(name.strip().lower())

    # Any slots this chunk did not cover (model returned fewer than asked).
    unfilled.extend(slots[len(parsed):])
    return unfilled


def build_llm_catalogue_sync(
    mix: ProviderMix, **kwargs: object
) -> tuple[list[CompanyRow], dict[str, list[ProductTemplate]], BuildReport]:
    """Blocking wrapper for the CLI, which is synchronous."""
    return asyncio.run(build_llm_catalogue(mix, **kwargs))  # type: ignore[arg-type]
