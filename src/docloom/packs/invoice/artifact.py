"""The catalogue artifact — content as a versioned, downloadable file.

A catalogue answers "which companies issue invoices, and what does each one
sell". :class:`~docloom.packs.invoice.catalog.SeedCatalogue` answers it from
constants in code, which caps the corpus at 25 product descriptions. This module
answers it from a **published Parquet artifact**, so the pool can grow to
hundreds of thousands of items without shipping them in the wheel.

The split that makes this work:

    [ built once, offline ]              [ every run, everywhere ]
    LLM or procedural build   ─────▸     load artifact, draw procedurally
    needs keys, auditable                NO keys, deterministic, computed golden

Generation never calls an LLM. The artifact is data, and reading data needs no
credential — so a rich corpus stays as local-first as the seed catalogue.

**Format: Parquet, organised per company.** ``pyproject.toml`` already calls
Parquet "the default golden format" and both pyarrow and duckdb are core
dependencies, so the artifact *is* the export format: DuckDB and BigQuery read it
directly with no conversion step.

    catalogues/invoice/v1/
      manifest.json        version, sha256 per file, row counts, provenance
      companies.parquet    the roster
      products.parquet     every company's SKUs, sorted by company_id

**Identity is not stored.** Addresses, phone numbers, emails and tax
registrations are regenerated from the company id by
:func:`~docloom.packs.invoice.catalog.derive_identity`. They are the
highest-PII-risk fields and need the least creativity, so keeping them out means
a published artifact has no PII-shaped field to leak — the audit surface is names
and product descriptions, nothing else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from docloom.core.locale.enums import Currency, Locale
from docloom.core.storage import open_store
from docloom.core.storage.base import BlobStore
from docloom.packs.invoice.catalog import (
    BusinessSpec,
    Company,
    CompanyRoster,
    ProductTemplate,
    derive_identity,
)
from docloom.packs.invoice.enums import (
    BillingModel,
    BusinessType,
    CodeSystem,
    LineItemKind,
    UsageUnit,
)
from docloom.packs.invoice.jurisdictions import Jurisdiction

#: Bumped when the Parquet column layout changes incompatibly. A reader refuses
#: an artifact it does not understand rather than mis-reading columns.
SCHEMA_VERSION = 1

MANIFEST_KEY = "manifest.json"
COMPANIES_KEY = "companies.parquet"
PRODUCTS_KEY = "products.parquet"

#: Money as decimal128, matching the golden pipeline (which already maps
#: decimal128 → BigQuery NUMERIC). Prices here are sampling *inputs* rather than
#: golden values, so a float would technically do — consistency is worth more
#: than the few bytes saved.
_MONEY_PRECISION, _MONEY_SCALE = 18, 4

#: Telecom bills hundreds of usage lines; everything else bills a handful. Same
#: rule the seed catalogue applies, kept here so an artifact needs no extra
#: column for it.
_TELECOM_LINES = (60, 400)
_DEFAULT_LINES = (3, 12)


@dataclass(frozen=True, slots=True)
class CompanyRow:
    """The storable half of a company — everything that is *not* identity."""

    company_id: str
    name: str
    business_type: BusinessType
    jurisdiction: Jurisdiction
    locale: Locale
    currency: Currency
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class CatalogueManifest:
    """What an artifact is, and proof of what it contains."""

    catalogue_version: str
    pack: str = "invoice"
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: How it was built — models used, dedup and regeneration rates, PII scan
    #: result. An artifact ships with its own audit.
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "catalogue_version": self.catalogue_version,
                "pack": self.pack,
                "schema_version": self.schema_version,
                "created_at": self.created_at,
                "files": self.files,
                "provenance": self.provenance,
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> CatalogueManifest:
        data = json.loads(raw)
        return cls(
            catalogue_version=data["catalogue_version"],
            pack=data.get("pack", "invoice"),
            schema_version=int(data.get("schema_version", 0)),
            created_at=data.get("created_at", ""),
            files=data.get("files", {}),
            provenance=data.get("provenance", {}),
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pa():  # noqa: ANN202
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - pyarrow is a core dependency
        raise ImportError("the catalogue artifact needs pyarrow") from exc
    return pa


def companies_schema():  # noqa: ANN201
    pa = _pa()
    return pa.schema([
        ("company_id", pa.string()),
        ("name", pa.string()),
        ("business_type", pa.string()),
        ("jurisdiction", pa.string()),
        ("locale", pa.string()),
        ("currency", pa.string()),
        ("weight", pa.float64()),
    ])


def products_schema():  # noqa: ANN201
    pa = _pa()
    money = pa.decimal128(_MONEY_PRECISION, _MONEY_SCALE)
    return pa.schema([
        ("company_id", pa.string()),
        ("sku_id", pa.string()),
        ("description", pa.string()),
        ("description_fr", pa.string()),
        ("kind", pa.string()),
        ("billing_model", pa.string()),
        ("code_system", pa.string()),
        ("code_prefix", pa.string()),
        ("usage_unit", pa.string()),
        ("price_low", money),
        ("price_high", money),
    ])


def _table_bytes(table) -> bytes:  # noqa: ANN001
    import io

    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def write_catalogue(
    uri: str,
    *,
    companies: Sequence[CompanyRow],
    products: Mapping[str, Sequence[ProductTemplate]],
    catalogue_version: str,
    provenance: dict[str, Any] | None = None,
) -> CatalogueManifest:
    """Write a catalogue artifact to ``uri`` and return its manifest.

    ``products`` is keyed by ``company_id``: catalogues are **per company**,
    because that is what realism means. A vendor sells its own SKUs over and over
    — global uniqueness is the wrong target, and per-company pools also partition
    by language for free, since a company has one locale.

    Products are written sorted by ``company_id`` so a reader can skip row groups
    for companies it does not need, without exploding the artifact into a
    directory per company.
    """
    pa = _pa()
    if not companies:
        raise ValueError("a catalogue needs at least one company")
    missing = [c.company_id for c in companies if not products.get(c.company_id)]
    if missing:
        raise ValueError(
            f"{len(missing)} company/companies have no products, e.g. {missing[:3]} — "
            "a company that sells nothing cannot issue an invoice"
        )

    companies_table = pa.Table.from_pydict(
        {
            "company_id": [c.company_id for c in companies],
            "name": [c.name for c in companies],
            "business_type": [str(c.business_type) for c in companies],
            "jurisdiction": [str(c.jurisdiction) for c in companies],
            "locale": [str(c.locale) for c in companies],
            "currency": [str(c.currency) for c in companies],
            "weight": [float(c.weight) for c in companies],
        },
        schema=companies_schema(),
    )

    rows: list[tuple[str, ProductTemplate, int]] = [
        (c.company_id, p, i)
        for c in sorted(companies, key=lambda c: c.company_id)
        for i, p in enumerate(products[c.company_id])
    ]
    quant = Decimal(1).scaleb(-_MONEY_SCALE)
    products_table = pa.Table.from_pydict(
        {
            "company_id": [cid for cid, _, _ in rows],
            "sku_id": [f"{cid}-{i:05d}" for cid, _, i in rows],
            "description": [p.description for _, p, _ in rows],
            "description_fr": [p.fr for _, p, _ in rows],
            "kind": [str(p.kind) for _, p, _ in rows],
            "billing_model": [str(p.billing_model) for _, p, _ in rows],
            "code_system": [str(p.code_system) for _, p, _ in rows],
            "code_prefix": [p.code_prefix for _, p, _ in rows],
            "usage_unit": [str(p.usage_unit) for _, p, _ in rows],
            "price_low": [p.price_low.quantize(quant) for _, p, _ in rows],
            "price_high": [p.price_high.quantize(quant) for _, p, _ in rows],
        },
        schema=products_schema(),
    )

    companies_bytes = _table_bytes(companies_table)
    products_bytes = _table_bytes(products_table)

    manifest = CatalogueManifest(
        catalogue_version=catalogue_version,
        created_at=datetime.now(UTC).isoformat(),
        files={
            COMPANIES_KEY: {"sha256": _sha256(companies_bytes), "rows": len(companies)},
            PRODUCTS_KEY: {"sha256": _sha256(products_bytes), "rows": len(rows)},
        },
        provenance=dict(provenance or {}),
    )

    blob = open_store(uri)
    blob.put(COMPANIES_KEY, companies_bytes, "application/vnd.apache.parquet")
    blob.put(PRODUCTS_KEY, products_bytes, "application/vnd.apache.parquet")
    # Manifest last: it is the completion marker. A reader that finds it can
    # trust the parquet files beside it are whole.
    blob.put(MANIFEST_KEY, manifest.to_json(), "application/json")
    return manifest


class ArtifactCatalogue:
    """A :class:`~docloom.packs.invoice.catalog.Catalogue` backed by an artifact.

    Loaded eagerly: at the sizes this serves (~300k products for a 1M-invoice
    corpus) the whole table is roughly 100 MB of Python objects, which a worker
    can hold comfortably, and a unit's documents touch enough distinct companies
    that lazy per-company loading would materialise most of it anyway. If that
    stops being true, the row groups are sorted by ``company_id`` precisely so a
    lazy reader can be added without changing the format.
    """

    def __init__(
        self,
        manifest: CatalogueManifest,
        companies: list[CompanyRow],
        products: dict[str, tuple[ProductTemplate, ...]],
    ) -> None:
        self._manifest = manifest
        self._products = products
        self._roster = CompanyRoster([_to_company(row) for row in companies])

    @property
    def version(self) -> str:
        return self._manifest.catalogue_version

    @property
    def manifest(self) -> CatalogueManifest:
        return self._manifest

    def roster(self) -> CompanyRoster:
        return self._roster

    def spec_for(self, company: Company) -> BusinessSpec:
        """This company's own products — the per-company half of the design."""
        low, high = (_TELECOM_LINES if company.business_type is BusinessType.TELECOM
                     else _DEFAULT_LINES)
        return BusinessSpec(
            business_type=company.business_type,
            products=self._products[company.company_id],
            line_count_low=low,
            line_count_high=high,
        )

    def business_spec(self, business_type: BusinessType) -> BusinessSpec:
        """Every product of that business type, pooled across companies.

        Present for interface compatibility with the seed catalogue; the sampler
        uses :meth:`spec_for`, which is what makes a company's invoices look like
        that company's.
        """
        pooled = tuple(
            p
            for company in self._roster.companies
            if company.business_type is business_type
            for p in self._products.get(company.company_id, ())
        )
        low, high = (_TELECOM_LINES if business_type is BusinessType.TELECOM
                     else _DEFAULT_LINES)
        return BusinessSpec(business_type, pooled, low, high)


def _to_company(row: CompanyRow) -> Company:
    party, profile = derive_identity(
        row.company_id, row.name, row.business_type, row.jurisdiction, row.locale
    )
    return Company(
        company_id=row.company_id,
        name=row.name,
        business_type=row.business_type,
        jurisdiction=row.jurisdiction,
        locale=row.locale,
        currency=row.currency,
        party=party,
        render_profile=profile,
        weight=row.weight,
    )


def load_catalogue(uri: str, *, verify: bool = True) -> ArtifactCatalogue:
    """Load a catalogue artifact from ``uri`` (``file://``, ``gs://``, ``s3://``).

    ``verify`` re-hashes each Parquet file against the manifest. On by default:
    an artifact is downloaded from somewhere, and a truncated or swapped file
    would otherwise surface as strange content rather than a clear error.
    """
    import io

    import pyarrow.parquet as pq

    blob = open_store(uri)
    try:
        manifest = CatalogueManifest.from_json(blob.get(MANIFEST_KEY))
    except KeyError as exc:
        raise FileNotFoundError(
            f"no catalogue manifest at {uri!r} — expected {MANIFEST_KEY} beside "
            f"{COMPANIES_KEY} and {PRODUCTS_KEY}"
        ) from exc

    if manifest.schema_version > SCHEMA_VERSION:
        raise ValueError(
            f"catalogue at {uri!r} uses schema version {manifest.schema_version}, "
            f"but this docloom understands up to {SCHEMA_VERSION} — upgrade docloom"
        )

    raw = {key: blob.get(key) for key in (COMPANIES_KEY, PRODUCTS_KEY)}
    if verify:
        for key, data in raw.items():
            expected = manifest.files.get(key, {}).get("sha256")
            actual = _sha256(data)
            if expected and expected != actual:
                raise ValueError(
                    f"{key} in {uri!r} does not match its manifest hash "
                    f"(expected {expected[:12]}…, got {actual[:12]}…) — the "
                    "artifact is truncated or was modified"
                )

    companies = [
        CompanyRow(
            company_id=r["company_id"],
            name=r["name"],
            business_type=BusinessType(r["business_type"]),
            jurisdiction=Jurisdiction(r["jurisdiction"]),
            locale=Locale(r["locale"]),
            currency=Currency(r["currency"]),
            weight=float(r["weight"]),
        )
        for r in pq.read_table(io.BytesIO(raw[COMPANIES_KEY])).to_pylist()
    ]

    products: dict[str, list[ProductTemplate]] = {}
    for r in pq.read_table(io.BytesIO(raw[PRODUCTS_KEY])).to_pylist():
        products.setdefault(r["company_id"], []).append(
            ProductTemplate(
                description=r["description"],
                price_low=r["price_low"],
                price_high=r["price_high"],
                fr=r["description_fr"] or "",
                kind=LineItemKind(r["kind"]),
                billing_model=BillingModel(r["billing_model"]),
                code_system=CodeSystem(r["code_system"]),
                code_prefix=r["code_prefix"],
                usage_unit=UsageUnit(r["usage_unit"]),
            )
        )

    orphans = [c.company_id for c in companies if c.company_id not in products]
    if orphans:
        raise ValueError(
            f"{len(orphans)} company/companies in {uri!r} have no products, "
            f"e.g. {orphans[:3]}"
        )

    return ArtifactCatalogue(
        manifest, companies, {k: tuple(v) for k, v in products.items()}
    )
