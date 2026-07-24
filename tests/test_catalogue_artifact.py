"""Catalogue artifact tests — content as a published, versioned file.

The artifact is what lifts the corpus past the seed catalogue's 25 product
descriptions. It is downloaded rather than shipped, so the load path has to be
strict: a truncated or swapped file must fail loudly, not surface as strange
content halfway through a million-document run.

The other property under test is the PII one, and it is structural rather than
procedural: identity fields are *not in the artifact at all*. There is no address
in the file to leak.
"""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from docloom.core.locale.enums import Currency, Locale
from docloom.packs.invoice.artifact import (
    COMPANIES_KEY,
    MANIFEST_KEY,
    PRODUCTS_KEY,
    SCHEMA_VERSION,
    CatalogueManifest,
    CompanyRow,
    load_catalogue,
    write_catalogue,
)
from docloom.packs.invoice.catalog import Catalogue, ProductTemplate, SeedCatalogue
from docloom.packs.invoice.enums import BillingModel, BusinessType, CodeSystem, LineItemKind
from docloom.packs.invoice.jurisdictions import Jurisdiction
from docloom.packs.invoice.sampler import InvoiceSampler


def a_company(cid: str, **kw) -> CompanyRow:  # noqa: ANN003
    base = dict(company_id=cid, name=f"{cid.title()} Trading Ltd",
                business_type=BusinessType.RETAIL, jurisdiction=Jurisdiction.US,
                locale=Locale.EN_US, currency=Currency.USD, weight=1.0)
    base.update(kw)
    return CompanyRow(**base)  # type: ignore[arg-type]


def some_products(n: int, prefix: str = "widget") -> list[ProductTemplate]:
    return [
        ProductTemplate(
            description=f"{prefix} number {i}",
            price_low=D("1.50"), price_high=D("9.99"),
            fr=f"{prefix} numéro {i}",
            kind=LineItemKind.PRODUCT, billing_model=BillingModel.PER_UNIT,
            code_system=CodeSystem.SKU, code_prefix="WD",
        )
        for i in range(n)
    ]


def write_small(tmp_path: Path, *, companies=2, products=6, version="v1"):  # noqa: ANN001, ANN201
    rows = [a_company(f"co{i}") for i in range(companies)]
    catalogue = {r.company_id: some_products(products, f"co{i}-item")
                 for i, r in enumerate(rows)}
    manifest = write_catalogue(str(tmp_path), companies=rows, products=catalogue,
                               catalogue_version=version,
                               provenance={"built_by": "test"})
    return manifest, rows, catalogue


# ── Round trip ──────────────────────────────────────────────────────────────
def test_write_then_load_round_trips(tmp_path: Path) -> None:
    _, rows, products = write_small(tmp_path, companies=3, products=4)
    loaded = load_catalogue(str(tmp_path))

    assert loaded.version == "v1"
    assert len(loaded.roster()) == 3
    for company in loaded.roster().companies:
        spec = loaded.spec_for(company)
        assert len(spec.products) == 4
        original = products[company.company_id]
        assert [p.description for p in spec.products] == [p.description for p in original]


def test_money_survives_as_exact_decimal(tmp_path: Path) -> None:
    """decimal128, not float — the golden pipeline maps decimal128 to BigQuery
    NUMERIC and a price band that drifts would produce amounts that do not
    reconcile."""
    rows = [a_company("co0")]
    products = {"co0": [ProductTemplate("thing", D("0.0001"), D("12345.6789"))]}
    write_catalogue(str(tmp_path), companies=rows, products=products,
                    catalogue_version="v1")
    product = load_catalogue(str(tmp_path)).spec_for(
        load_catalogue(str(tmp_path)).roster().companies[0]
    ).products[0]
    assert product.price_low == D("0.0001")
    assert product.price_high == D("12345.6789")
    assert isinstance(product.price_low, D)


def test_the_artifact_satisfies_the_catalogue_protocol(tmp_path: Path) -> None:
    write_small(tmp_path)
    assert isinstance(load_catalogue(str(tmp_path)), Catalogue)
    assert isinstance(SeedCatalogue(), Catalogue)


def test_products_are_per_company_not_pooled(tmp_path: Path) -> None:
    """The whole point of per-company catalogues: a vendor sells its own SKUs."""
    _, _, products = write_small(tmp_path, companies=3, products=5)
    loaded = load_catalogue(str(tmp_path))
    seen = {c.company_id: {p.description for p in loaded.spec_for(c).products}
            for c in loaded.roster().companies}
    assert len(seen) == 3
    # No company sees another's catalogue.
    for cid, descriptions in seen.items():
        others = set().union(*(v for k, v in seen.items() if k != cid))
        assert not (descriptions & others)


# ── Identity is derived, never stored ───────────────────────────────────────
def test_no_pii_shaped_field_is_written_to_the_artifact(tmp_path: Path) -> None:
    """The structural PII guarantee: addresses, phones, emails and tax ids are
    not in the file, so a published artifact has nothing of that kind to leak."""
    write_small(tmp_path)
    import pyarrow.parquet as pq

    columns = set(pq.read_table(tmp_path / COMPANIES_KEY).column_names)
    for forbidden in ("address", "address_lines", "phone", "email", "website",
                      "registrations", "city"):
        assert forbidden not in columns, f"{forbidden!r} must not be stored"
    assert columns == {"company_id", "name", "business_type", "product_category",
                       "jurisdiction", "locale", "currency", "weight"}


def test_identity_is_derived_and_stable(tmp_path: Path) -> None:
    """Not stored, but still deterministic: the same company id yields the same
    address every time, so a vendor looks like itself across runs."""
    write_small(tmp_path)
    first = {c.company_id: c.party.city for c in load_catalogue(str(tmp_path)).roster().companies}
    second = {c.company_id: c.party.city for c in load_catalogue(str(tmp_path)).roster().companies}
    assert first == second
    assert all(city for city in first.values())


def test_derived_contact_details_use_reserved_ranges(tmp_path: Path) -> None:
    write_small(tmp_path)
    for company in load_catalogue(str(tmp_path)).roster().companies:
        assert "555-" in (company.party.phone or "")
        assert (company.party.email or "").endswith(".example")


# ── Integrity ───────────────────────────────────────────────────────────────
def test_a_tampered_file_is_rejected(tmp_path: Path) -> None:
    """An artifact is downloaded from somewhere. A truncated or swapped file must
    fail loudly rather than surfacing as strange content mid-run."""
    write_small(tmp_path)
    (tmp_path / PRODUCTS_KEY).write_bytes(b"not a parquet file")
    with pytest.raises(ValueError, match="does not match its manifest hash"):
        load_catalogue(str(tmp_path))


def test_verification_can_be_skipped(tmp_path: Path) -> None:
    """Hashing 300k rows on every worker start is worth skipping when the
    artifact is already known-good and local."""
    write_small(tmp_path)
    assert load_catalogue(str(tmp_path), verify=False).version == "v1"


def test_a_missing_manifest_is_a_clear_error(tmp_path: Path) -> None:
    write_small(tmp_path)
    (tmp_path / MANIFEST_KEY).unlink()
    with pytest.raises(FileNotFoundError, match="no catalogue manifest"):
        load_catalogue(str(tmp_path))


def test_a_future_schema_version_is_refused(tmp_path: Path) -> None:
    """Better to say 'upgrade docloom' than to mis-read columns."""
    write_small(tmp_path)
    manifest = json.loads((tmp_path / MANIFEST_KEY).read_text())
    manifest["schema_version"] = SCHEMA_VERSION + 5
    (tmp_path / MANIFEST_KEY).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="upgrade docloom"):
        load_catalogue(str(tmp_path))


def test_the_manifest_records_hashes_counts_and_provenance(tmp_path: Path) -> None:
    manifest, _, _ = write_small(tmp_path, companies=2, products=3)
    assert manifest.files[COMPANIES_KEY]["rows"] == 2
    assert manifest.files[PRODUCTS_KEY]["rows"] == 6
    assert len(manifest.files[PRODUCTS_KEY]["sha256"]) == 64
    assert manifest.provenance["built_by"] == "test"
    assert CatalogueManifest.from_json(manifest.to_json()) == manifest


def test_a_company_with_no_products_is_rejected_at_write(tmp_path: Path) -> None:
    """A company that sells nothing cannot issue an invoice — catch it while
    building, not on the first document of a million-document run."""
    with pytest.raises(ValueError, match="no products"):
        write_catalogue(str(tmp_path), companies=[a_company("co0")], products={},
                        catalogue_version="v1")


# ── Generating from an artifact ─────────────────────────────────────────────
def test_a_sampler_generates_from_an_artifact(tmp_path: Path) -> None:
    write_small(tmp_path, companies=2, products=8)
    sampler = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=6)
    invoices = [sampler.generate("artifact-run", i) for i in range(10)]

    assert all(inv.line_items for inv in invoices)
    assert all(inv.totals.grand_total > 0 for inv in invoices)
    # Every description came from the artifact, not the built-in seed pool.
    assert all("item number" in li.description or "item numéro" in li.description
               for inv in invoices for li in inv.line_items)


def test_the_catalogue_version_reaches_the_golden_row(tmp_path: Path) -> None:
    """A corpus is only reproducible if it records which content produced it."""
    write_small(tmp_path, version="catalogue-2026-07")
    sampler = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=4)
    invoice = sampler.generate("r", 0)
    assert invoice.catalogue_version == "catalogue-2026-07"
    assert invoice.to_rows()["invoices"][0]["catalogue_version"] == "catalogue-2026-07"


def test_the_seed_catalogue_reports_its_own_version() -> None:
    assert InvoiceSampler(max_line_items=4).generate("r", 0).catalogue_version == "seed-1"


def test_generation_from_an_artifact_is_deterministic(tmp_path: Path) -> None:
    write_small(tmp_path, companies=3, products=6)
    a = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=6).generate("d", 3)
    b = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=6).generate("d", 3)
    assert [li.description for li in a.line_items] == [li.description for li in b.line_items]
    assert a.totals.grand_total == b.totals.grand_total
    assert a.issuer.city == b.issuer.city


def test_a_french_company_prints_french_descriptions(tmp_path: Path) -> None:
    rows = [a_company("fr0", jurisdiction=Jurisdiction.FR, locale=Locale.FR_FR,
                      currency=Currency.EUR)]
    write_catalogue(str(tmp_path), companies=rows,
                    products={"fr0": some_products(5, "article")},
                    catalogue_version="v1")
    sampler = InvoiceSampler(load_catalogue(str(tmp_path)), max_line_items=5)
    invoice = sampler.generate("fr", 0)
    assert str(invoice.locale) == "fr-FR"
    assert all("numéro" in li.description for li in invoice.line_items)
