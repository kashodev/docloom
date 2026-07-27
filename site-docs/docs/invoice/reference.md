# Invoice pack — Reference

Every module in `packs/invoice/`, and where it is covered in depth.

| Module | Purpose | Covered in |
|---|---|---|
| `__init__.py` | `InvoicePack` — implements [`DocumentPack`](../architecture/contract.md). | [Overview](overview.md) |
| `sampler.py` | `InvoiceSampler` — the pack's `DocumentSource`; per-index deterministic invoice generation. | [Generation](generation.md) |
| `record.py` | `GoldenInvoice` + parts: `Party`, `LineItem`, `PricingTier`, `TaxBucket`, `InvoiceTotals`, `RenderProfile`. | [Generation](generation.md) |
| `context.py` | Golden invoice → Jinja context (`build_context`). | [Generation](generation.md) |
| `catalog.py` | `Catalogue` / `CompanyRoster` / `Company` / `ProductTemplate`; `SeedCatalogue`; `derive_identity`. | [Content](content.md) |
| `procedural.py` | Key-free combinatorial catalogue; `generate_company[_range]`. | [Content](content.md) |
| `llm_build.py` | LLM overlay: prompt, parse, price-band coercion, procedural fallback. | [Content](content.md) |
| `build_run.py` | Distributed catalogue build = a run over company ranges (`build_catalogue_run`). | [Catalogue building](catalogue.md) |
| `artifact.py` | Versioned artifact: per-company Parquet, decimal128, sha256, sharded read/write, manifest. | [Content](content.md) |
| `validation.py` | Quality + PII gates on catalogue content. | [Content](content.md) |
| `composition.py` | Resolve a `Selection` against the roster; constraint validation. | [Composition](composition.md) |
| `enums.py` | `BusinessType`, `BillingModel`, `LineItemKind`, `UsageUnit`, `DiscountScheme`, … | this page |
| `jurisdictions.py` | Tax model, currency, mandatory issuer fields per jurisdiction. | [Composition](composition.md) |
| `labels.py` | Printed label dictionaries per locale. | [Rendering](rendering.md) |
| `handwriting.py` | Handwriting params (fonts, jitter) for the handwritten pad. | [Rendering](rendering.md) |
| `logos.py` | Procedural key-free brand marks + watermark. | [Rendering](rendering.md) |
| `stamps.py` | Procedural company seals/stamps. | [Rendering](rendering.md) |
| `templates/` | `_base`, `_macros`, `_styles`, and 7 archetypes. | [Rendering](rendering.md) |

## Vocabularies (`enums.py`)

- **`BusinessType`** — `retail`, `ecommerce`, `grocery`, `wholesale`, `auto_repair`,
  `construction`, `manufacturing`, `b2b_saas`, `ai_platform`, `telecom`,
  `accounting`, `legal`, `consulting`, `healthcare`, `logistics`, `utilities`.
- **`BillingModel`** — `flat_rate`, `per_unit`, `graduated_tier`, `volume_tier`,
  `metered_usage`, `subscription`, `seat_based`, `hourly_labour`, `milestone`,
  `interest`.
- `LineItemKind`, `UsageUnit`, `DiscountScheme` — the line-item and pricing
  vocabularies the sampler draws from.

The kernel vocabularies a pack shares (`Jurisdiction`, `DocumentCondition`,
`RunState`, `WorkUnitState`) live in `core/enums.py` — see
[Core → Locale / render / money / logging](../core/misc.md).
