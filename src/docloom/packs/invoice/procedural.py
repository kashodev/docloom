"""Procedural catalogue generation — a large content pool for nothing.

Builds a catalogue artifact by **combinatorial expansion**: a product name is
assembled from attribute slots (material × form × size × pack), so a few dozen
hand-written parts yield tens of thousands of distinct descriptions. Company
names are assembled the same way from invented stems.

Why this exists before the LLM build:

* **It proves the pipeline at zero cost.** Writing, loading, versioning and
  generating from an artifact is exercised end to end with no key and no spend,
  so the expensive step is an enhancement rather than a dependency.
* **It is most of the realism.** The corpus is for testing extraction, which is
  sensitive to token shapes, lengths, casing, punctuation and diacritics — not to
  whether a product plausibly exists. Combinatorics deliver that variety; the LLM
  adds semantic long-tail on top.
* **PII risk is structurally nil.** Every string is assembled from invented
  parts, so nothing can be memorised from training data. Recombination is what
  makes names novel *by construction* rather than by promise.

Deterministic from a seed: the same seed yields the same catalogue, so a
published artifact can be rebuilt and verified rather than trusted.
"""

from __future__ import annotations

from decimal import Decimal
from random import Random

from docloom.core.locale.enums import Currency, Locale
from docloom.core.pipeline.source import stable_seed
from docloom.packs.invoice.artifact import CompanyRow
from docloom.packs.invoice.catalog import ProductTemplate
from docloom.packs.invoice.enums import (
    BillingModel,
    BusinessType,
    CodeSystem,
    LineItemKind,
    UsageUnit,
)
from docloom.packs.invoice.jurisdictions import Jurisdiction

# ── Company name stems ──────────────────────────────────────────────────────
# Invented, not drawn from any register. Recombination is the point: a real
# company name cannot survive being split into parts and reassembled against
# different parts, so novelty is structural rather than promised.
_STEM_A = (
    "North", "Cedar", "Iron", "Blue", "Van", "Meri", "Aur", "Copper", "Summit",
    "Harbor", "Cres", "Bram", "Halcy", "Fox", "Gran", "Willow", "Tana", "Ash",
    "Bea", "Kes", "Alder", "Ember", "Fen", "Glim", "Hollow", "Juni", "Larch",
    "Marl", "Nim", "Orch", "Quill", "Rowan", "Slate", "Thistle", "Umber", "Verd",
)
_STEM_B = (
    "wind", "peak", "clad", "tage", "dian", "ora", "line", "bury", "gate",
    "field", "ridge", "brook", "haven", "mark", "stone", "vale", "wick", "ford",
    "crest", "moor", "shaw", "dell", "thorn", "reach",
)
_SUFFIX = {
    Jurisdiction.US: ("Inc.", "LLC", "Co.", "Corp.", "Holdings"),
    Jurisdiction.CA_ON: ("Inc.", "Ltd.", "Group"),
    Jurisdiction.CA_QC: ("inc.", "ltée", "Groupe"),
    Jurisdiction.CA_BC: ("Inc.", "Ltd."),
    Jurisdiction.CA_AB: ("Inc.", "Ltd."),
    Jurisdiction.GB: ("Ltd", "PLC", "Group", "& Co"),
    Jurisdiction.FR: ("SARL", "SAS", "SA", "et Cie"),
}

#: Jurisdiction → (locale, currency). Currency follows jurisdiction, which is why
#: pinning a locale is how a run asks for GBP or EUR.
_MARKETS: tuple[tuple[Jurisdiction, Locale, Currency], ...] = (
    (Jurisdiction.US, Locale.EN_US, Currency.USD),
    (Jurisdiction.CA_ON, Locale.EN_CA, Currency.CAD),
    (Jurisdiction.CA_BC, Locale.EN_CA, Currency.CAD),
    (Jurisdiction.GB, Locale.EN_GB, Currency.GBP),
    (Jurisdiction.FR, Locale.FR_FR, Currency.EUR),
    (Jurisdiction.CA_QC, Locale.FR_CA, Currency.CAD),
)


# ── Merchant sub-categories ─────────────────────────────────────────────────
# A company sells ONE narrow, coherent product family — not its whole umbrella
# business type. "Retail" spans hardware, apparel and electronics; a single shop
# sells one line. Prompting the LLM (or drawing procedural slots) on the umbrella
# is what produced invoices mixing compression shorts and a motherboard, so the
# **family** is the unit of coherence: procedural slots key off it and the LLM
# prompt names it. A company's family is fixed by its seed (see generate_company)
# and stored on the row, so the whole catalogue is coherent per company.
CAT_HARDWARE = "hardware and fasteners"
CAT_OFFICE = "office and IT supplies"
CAT_APPAREL = "apparel and accessories"
CAT_KITCHEN = "home and kitchenware"
CAT_PACKAGING = "packaging and shipping supplies"
CAT_AUTO = "auto parts and repair"
CAT_SAAS = "software subscriptions"
CAT_ACCOUNTING = "accounting and tax services"
CAT_AI = "AI platform usage"
CAT_TELECOM = "telecom services"

# ── Product slots, per sub-category ─────────────────────────────────────────
# Each entry is (materials, forms, variants, unit-or-pack), combined into
# "<material> <form>, <variant> (<pack>)". A French rendering is built from the
# parallel table so a French company's catalogue is written in French — the
# artifact stores both and the sampler picks by locale.
_SLOTS: dict[str, dict[str, tuple[str, ...]]] = {
    CAT_HARDWARE: {
        "material": ("Stainless steel", "Galvanised", "Brass", "Nylon", "Copper",
                     "Aluminium", "Zinc-plated", "Carbon steel"),
        "form": ("hex bolt", "washer set", "socket cap screw", "hinge", "bracket",
                 "clamp", "anchor", "wing nut", "threaded rod", "split pin"),
        "variant": ("M6 x 25mm", "M8 x 40mm", "M10 x 60mm", "1/4 inch", "3/8 inch",
                    "12mm", "20mm", "50mm"),
        "pack": ("pack of 10", "pack of 50", "box of 100", "each", "bag of 25"),
    },
    CAT_OFFICE: {
        "material": ("Standard", "Premium", "Compact", "Heavy-duty", "Executive",
                     "Basic", "Deluxe"),
        "form": ("office chair", "desk lamp", "keyboard", "monitor stand",
                 "filing cabinet", "paper shredder", "label maker", "docking station",
                 "USB hub", "webcam", "desk organiser", "whiteboard"),
        "variant": ("black", "grey", "silver", "white", "standard", "large"),
        "pack": ("each", "unit", "set", "pack of 2"),
    },
    CAT_APPAREL: {
        "material": ("Cotton", "Wool", "Denim", "Polyester", "Linen", "Fleece",
                     "Leather"),
        "form": ("t-shirt", "jeans", "dress shirt", "hoodie", "socks", "jacket",
                 "shorts", "scarf", "cap", "belt"),
        "variant": ("small", "medium", "large", "black", "navy", "grey"),
        "pack": ("each", "pack of 3", "pair"),
    },
    CAT_KITCHEN: {
        "material": ("Stainless steel", "Ceramic", "Cast iron", "Bamboo", "Glass",
                     "Silicone", "Copper", "Nonstick"),
        "form": ("skillet", "mixing bowl", "cutting board", "knife set",
                 "storage container", "measuring cups", "baking sheet", "colander",
                 "utensil set", "food storage jar"),
        "variant": ("small", "medium", "large", "10 inch", "12 inch", "set of 4"),
        "pack": ("each", "set", "set of 4", "pack of 2"),
    },
    CAT_PACKAGING: {
        "material": ("Corrugated", "Kraft", "Polythene", "Shrink", "Bubble",
                     "Foam-lined", "Recycled"),
        "form": ("shipping carton", "mailer", "pallet wrap", "void fill", "strapping",
                 "stretch film", "edge protector", "gaylord liner"),
        "variant": ("12x12x8", "18-inch", "24-inch", "single wall", "double wall",
                    "heavy duty", "80 micron"),
        "pack": ("bundle of 25", "case of 50", "roll", "pallet", "carton of 200"),
    },
    CAT_AUTO: {
        "material": ("Ceramic", "Semi-metallic", "Synthetic", "OEM", "Heavy-duty",
                     "Performance"),
        "form": ("brake pad set", "oil filter", "air filter", "wiper blade",
                 "spark plug", "serpentine belt", "wheel bearing", "shock absorber"),
        "variant": ("front axle", "rear axle", "left hand", "right hand", "16 inch",
                    "standard fit"),
        "pack": ("each", "pair", "set of 4", "kit"),
    },
    CAT_SAAS: {
        "material": ("Starter", "Growth", "Business", "Scale", "Enterprise"),
        "form": ("plan", "workspace tier", "seat bundle", "support package",
                 "onboarding package", "audit add-on"),
        "variant": ("monthly", "annual", "per seat", "per workspace", "prepaid"),
        "pack": ("subscription", "licence", "add-on"),
    },
    CAT_ACCOUNTING: {
        "material": ("Federal", "Provincial", "Corporate", "Personal", "Quarterly",
                     "Year-end"),
        "form": ("tax preparation", "bookkeeping", "payroll run", "advisory consultation",
                 "audit support", "compliance review"),
        "variant": ("standard", "expedited", "multi-entity", "first filing"),
        "pack": ("engagement", "hourly", "monthly retainer"),
    },
    CAT_AI: {
        "material": ("Input", "Output", "Cached", "Batch", "Fine-tuning", "Embedding"),
        "form": ("tokens", "API requests", "GPU inference hours", "vector storage",
                 "training steps"),
        "variant": ("standard tier", "priority tier", "reserved capacity", "spot"),
        "pack": ("per million", "per thousand", "per hour"),
    },
    CAT_TELECOM: {
        "material": ("Mobile", "Roaming", "International", "Domestic", "Premium"),
        "form": ("data", "voice minutes", "text messages", "video calling",
                 "hotspot data"),
        "variant": ("peak", "off-peak", "weekend", "zone 1", "zone 2"),
        "pack": ("per MB", "per minute", "per message"),
    },
}

_FR_MATERIAL = {
    "Stainless steel": "en acier inoxydable", "Galvanised": "galvanisé",
    "Brass": "en laiton", "Nylon": "en nylon", "Copper": "en cuivre",
    "Aluminium": "en aluminium", "Zinc-plated": "zingué", "Carbon steel": "en acier carbone",
    "Corrugated": "ondulé", "Kraft": "kraft", "Polythene": "en polyéthylène",
    "Shrink": "rétractable", "Bubble": "à bulles", "Foam-lined": "doublé de mousse",
    "Recycled": "recyclé", "Ceramic": "en céramique", "Semi-metallic": "semi-métallique",
    "Synthetic": "synthétique", "OEM": "d'origine", "Heavy-duty": "robuste",
    "Performance": "haute performance", "Starter": "Découverte", "Growth": "Croissance",
    "Business": "Affaires", "Scale": "Expansion", "Enterprise": "Entreprise",
    "Federal": "fédérale", "Provincial": "provinciale", "Corporate": "d'entreprise",
    "Personal": "personnelle", "Quarterly": "trimestrielle", "Year-end": "de fin d'exercice",
    "Input": "d'entrée", "Output": "de sortie", "Cached": "en cache", "Batch": "par lot",
    "Fine-tuning": "d'ajustement", "Embedding": "d'incorporation", "Mobile": "mobiles",
    "Roaming": "en itinérance", "International": "internationaux", "Domestic": "nationaux",
    "Premium": "privilégiés",
    # Office qualifiers, apparel fabrics, and kitchenware materials.
    "Standard": "standard", "Compact": "compact", "Executive": "de direction",
    "Basic": "basique", "Deluxe": "de luxe", "Cotton": "en coton",
    "Wool": "en laine", "Denim": "en denim", "Polyester": "en polyester",
    "Linen": "en lin", "Fleece": "en polaire", "Leather": "en cuir",
    "Cast iron": "en fonte", "Bamboo": "en bambou", "Glass": "en verre",
    "Silicone": "en silicone", "Nonstick": "antiadhésif",
}
_FR_FORM = {
    "hex bolt": "Boulon à tête hexagonale", "washer set": "Jeu de rondelles",
    "socket cap screw": "Vis à tête cylindrique", "hinge": "Charnière",
    "bracket": "Support", "clamp": "Collier", "anchor": "Cheville",
    "wing nut": "Écrou à oreilles", "threaded rod": "Tige filetée",
    "split pin": "Goupille fendue", "shipping carton": "Carton d'expédition",
    "mailer": "Pochette d'envoi", "pallet wrap": "Film étirable", "void fill": "Calage",
    "strapping": "Feuillard", "stretch film": "Film étirable",
    "edge protector": "Protège-angle", "gaylord liner": "Doublure de caisse-palette",
    "brake pad set": "Jeu de plaquettes de frein", "oil filter": "Filtre à huile",
    "air filter": "Filtre à air", "wiper blade": "Balai d'essuie-glace",
    "spark plug": "Bougie d'allumage", "serpentine belt": "Courroie d'accessoires",
    "wheel bearing": "Roulement de roue", "shock absorber": "Amortisseur",
    "plan": "Forfait", "workspace tier": "Niveau d'espace de travail",
    "seat bundle": "Lot de postes", "support package": "Offre de soutien",
    "onboarding package": "Offre de mise en service", "audit add-on": "Option d'audit",
    "tax preparation": "Préparation de déclaration", "bookkeeping": "Tenue de livres",
    "payroll run": "Traitement de la paie", "advisory consultation": "Consultation-conseil",
    "audit support": "Soutien à l'audit", "compliance review": "Revue de conformité",
    "tokens": "Jetons", "API requests": "Requêtes API",
    "GPU inference hours": "Heures d'inférence GPU", "vector storage": "Stockage vectoriel",
    "training steps": "Étapes d'entraînement", "data": "Données",
    "voice minutes": "Minutes d'appel", "text messages": "Messages texte",
    "video calling": "Appels vidéo", "hotspot data": "Données de partage de connexion",
    # Office & IT.
    "office chair": "Chaise de bureau", "desk lamp": "Lampe de bureau",
    "keyboard": "Clavier", "monitor stand": "Support d'écran",
    "filing cabinet": "Classeur", "paper shredder": "Déchiqueteuse",
    "label maker": "Étiqueteuse", "docking station": "Station d'accueil",
    "USB hub": "Concentrateur USB", "webcam": "Webcam",
    "desk organiser": "Organiseur de bureau", "whiteboard": "Tableau blanc",
    # Apparel & accessories.
    "t-shirt": "T-shirt", "jeans": "Jean", "dress shirt": "Chemise habillée",
    "hoodie": "Sweat à capuche", "socks": "Chaussettes", "jacket": "Veste",
    "shorts": "Short", "scarf": "Écharpe", "cap": "Casquette", "belt": "Ceinture",
    # Home & kitchenware.
    "skillet": "Poêle", "mixing bowl": "Saladier", "cutting board": "Planche à découper",
    "knife set": "Jeu de couteaux", "storage container": "Récipient de rangement",
    "measuring cups": "Tasses à mesurer", "baking sheet": "Plaque de cuisson",
    "colander": "Passoire", "utensil set": "Jeu d'ustensiles",
    "food storage jar": "Bocal de conservation",
}

#: Price bands per business type: (low, high) for the cheapest slot, scaled per
#: product so a catalogue spans plausible magnitudes rather than one price point.
_PRICE_BANDS: dict[BusinessType, tuple[str, str]] = {
    BusinessType.RETAIL: ("0.30", "220.00"),
    BusinessType.WHOLESALE: ("6.00", "180.00"),
    BusinessType.AUTO_REPAIR: ("9.00", "480.00"),
    BusinessType.B2B_SAAS: ("12.00", "1800.00"),
    BusinessType.ACCOUNTING: ("90.00", "2400.00"),
    BusinessType.AI_PLATFORM: ("0.0004", "6.00"),
    BusinessType.TELECOM: ("0.01", "0.60"),
}

#: Billing shape per business type — what the sampler needs to build a line.
_BILLING: dict[BusinessType, tuple[LineItemKind, BillingModel, CodeSystem, UsageUnit]] = {
    BusinessType.RETAIL: (LineItemKind.PRODUCT, BillingModel.PER_UNIT, CodeSystem.SKU,
                          UsageUnit.NONE),
    BusinessType.WHOLESALE: (LineItemKind.PRODUCT, BillingModel.PER_UNIT,
                             CodeSystem.UNSPSC, UsageUnit.NONE),
    BusinessType.AUTO_REPAIR: (LineItemKind.PART, BillingModel.PER_UNIT, CodeSystem.MPN,
                               UsageUnit.NONE),
    BusinessType.B2B_SAAS: (LineItemKind.SUBSCRIPTION, BillingModel.SUBSCRIPTION,
                            CodeSystem.NONE, UsageUnit.NONE),
    BusinessType.ACCOUNTING: (LineItemKind.SERVICE, BillingModel.FLAT_RATE,
                              CodeSystem.NONE, UsageUnit.NONE),
    BusinessType.AI_PLATFORM: (LineItemKind.USAGE, BillingModel.METERED_USAGE,
                               CodeSystem.NONE, UsageUnit.TOKENS_INPUT),
    BusinessType.TELECOM: (LineItemKind.USAGE, BillingModel.METERED_USAGE,
                           CodeSystem.NONE, UsageUnit.MEGABYTES),
}

# Which sub-categories each business type sells. A company draws exactly one, so
# its whole catalogue is one coherent line. Narrow types have a single family;
# `retail` is the broad one that otherwise drifts, so it fans out — different
# retail companies become different kinds of shop, each internally coherent.
_SUBCATEGORIES: dict[BusinessType, tuple[str, ...]] = {
    BusinessType.RETAIL: (CAT_HARDWARE, CAT_OFFICE, CAT_APPAREL, CAT_KITCHEN),
    BusinessType.WHOLESALE: (CAT_PACKAGING,),
    BusinessType.AUTO_REPAIR: (CAT_AUTO,),
    BusinessType.B2B_SAAS: (CAT_SAAS,),
    BusinessType.ACCOUNTING: (CAT_ACCOUNTING,),
    BusinessType.AI_PLATFORM: (CAT_AI,),
    BusinessType.TELECOM: (CAT_TELECOM,),
}

#: The business types a catalogue spans (the round-robin the sampler cycles).
BUSINESS_TYPES: tuple[BusinessType, ...] = tuple(_SUBCATEGORIES)


def combination_space(category: str) -> int:
    """How many distinct descriptions the slots can express for a sub-category."""
    slots = _SLOTS[category]
    total = 1
    for values in slots.values():
        total *= len(values)
    return total


def company_name_space() -> int:
    """Distinct company names expressible, before jurisdiction suffixes."""
    return len(_STEM_A) * len(_STEM_B)


def _describe(slots: dict[str, tuple[str, ...]], picks: dict[str, str]) -> str:
    return (f"{picks['material']} {picks['form']}, {picks['variant']} "
            f"({picks['pack']})")


_FR_VARIANT = {
    "front axle": "essieu avant", "rear axle": "essieu arrière", "left hand": "côté gauche",
    "right hand": "côté droit", "16 inch": "16 pouces", "standard fit": "montage standard",
    "single wall": "simple cannelure", "double wall": "double cannelure",
    "heavy duty": "robuste", "80 micron": "80 microns", "18-inch": "18 pouces",
    "24-inch": "24 pouces", "1/4 inch": "1/4 pouce", "3/8 inch": "3/8 pouce",
    # Dimensions are not language-neutral: French puts a space before the unit.
    "M6 x 25mm": "M6 x 25 mm", "M8 x 40mm": "M8 x 40 mm", "M10 x 60mm": "M10 x 60 mm",
    "12mm": "12 mm", "20mm": "20 mm", "50mm": "50 mm", "12x12x8": "12x12x8",
    "monthly": "mensuel", "annual": "annuel", "per seat": "par poste",
    "per workspace": "par espace de travail", "prepaid": "prépayé",
    "standard": "standard", "expedited": "accéléré", "multi-entity": "multi-entités",
    "first filing": "première déclaration", "standard tier": "niveau standard",
    "priority tier": "niveau prioritaire", "reserved capacity": "capacité réservée",
    "spot": "à la demande", "peak": "heures pleines", "off-peak": "heures creuses",
    "weekend": "fin de semaine", "zone 1": "zone 1", "zone 2": "zone 2",
    # Colours and sizes shared by the retail families.
    "black": "noir", "grey": "gris", "silver": "argent", "white": "blanc",
    "navy": "bleu marine", "small": "petit", "medium": "moyen", "large": "grand",
    "10 inch": "10 pouces", "12 inch": "12 pouces", "set of 4": "jeu de 4",
}
_FR_PACK = {
    "pack of 10": "paquet de 10", "pack of 50": "paquet de 50", "box of 100": "boîte de 100",
    "each": "à l'unité", "bag of 25": "sachet de 25", "bundle of 25": "paquet de 25",
    "case of 50": "caisse de 50", "roll": "rouleau", "pallet": "palette",
    "carton of 200": "carton de 200", "pair": "la paire", "set of 4": "jeu de 4",
    "kit": "trousse", "subscription": "abonnement", "licence": "licence",
    "add-on": "option", "engagement": "mandat", "hourly": "à l'heure",
    "monthly retainer": "forfait mensuel", "per million": "par million",
    "per thousand": "par millier", "per hour": "par heure", "per MB": "par Mo",
    "per minute": "par minute", "per message": "par message",
    "unit": "unité", "set": "jeu", "pack of 2": "paquet de 2", "pack of 3": "paquet de 3",
}


def _describe_fr(picks: dict[str, str]) -> str:
    """French rendering of every slot.

    All four slots are translated, not just the noun. A half-translated string
    ("Consultation-conseil d'entreprise, expedited (engagement)") is worse than
    either language alone: it is not text a French extractor would ever meet, so
    it tests nothing and looks broken. Any slot without a translation falls back
    to English, and a test asserts the tables are complete so the fallback never
    fires in practice.
    """
    form = _FR_FORM.get(picks["form"], picks["form"])
    material = _FR_MATERIAL.get(picks["material"], picks["material"])
    variant = _FR_VARIANT.get(picks["variant"], picks["variant"])
    pack = _FR_PACK.get(picks["pack"], picks["pack"])
    return f"{form} {material}, {variant} ({pack})"


def untranslated_slots() -> dict[str, list[str]]:
    """Slot values with no French rendering — empty when the tables are complete.

    Exposed so a test can fail on an added English value that nobody translated,
    rather than letting mixed-language text reach a French corpus.
    """
    missing: dict[str, list[str]] = {}
    tables = {"material": _FR_MATERIAL, "form": _FR_FORM,
              "variant": _FR_VARIANT, "pack": _FR_PACK}
    for slots in _SLOTS.values():
        for slot, values in slots.items():
            gaps = [v for v in values if v not in tables[slot]]
            if gaps:
                missing.setdefault(slot, []).extend(gaps)
    return {k: sorted(set(v)) for k, v in missing.items()}


def generate_products(
    rng: Random, category: str, business_type: BusinessType, count: int
) -> list[ProductTemplate]:
    """``count`` distinct products for one company, all in one sub-``category``.

    The product *family* is the sub-category (its slots); the *billing* shape
    (line kind, code system, price band, SKU prefix) still follows the umbrella
    ``business_type`` — apparel and hardware are both retail PER_UNIT SKUs.

    Distinct within the company is the property that matters — a vendor's
    catalogue should not list the same SKU twice — so combinations are drawn
    without replacement. Asking for more than the slots can express raises rather
    than silently returning duplicates.
    """
    slots = _SLOTS[category]
    space = combination_space(category)
    if count > space:
        raise ValueError(
            f"{category!r} can express {space} distinct products, {count} asked for — "
            "add slot values or lower the per-company count"
        )
    kind, billing, code_system, usage_unit = _BILLING[business_type]
    low, high = (Decimal(v) for v in _PRICE_BANDS[business_type])

    seen: set[tuple[str, ...]] = set()
    products: list[ProductTemplate] = []
    while len(products) < count:
        picks = {slot: rng.choice(values) for slot, values in slots.items()}
        key = tuple(picks[s] for s in slots)
        if key in seen:
            continue
        seen.add(key)
        # A per-product band inside the type's range, so one catalogue spans
        # magnitudes instead of clustering on a single price.
        span = high - low
        base = low + span * Decimal(str(rng.random())) * Decimal("0.9")
        products.append(
            ProductTemplate(
                description=_describe(slots, picks),
                price_low=base.quantize(Decimal("0.0001")),
                price_high=(base * Decimal(str(rng.uniform(1.1, 2.2)))).quantize(
                    Decimal("0.0001")
                ),
                fr=_describe_fr(picks),
                kind=kind,
                billing_model=billing,
                code_system=code_system,
                code_prefix=business_type.value[:3].upper(),
                usage_unit=usage_unit,
            )
        )
    return products


def generate_company(
    index: int,
    *,
    seed: int = 0,
    products_per_company: int = 300,
    business_types: tuple[BusinessType, ...] = BUSINESS_TYPES,
) -> tuple[CompanyRow, list[ProductTemplate]]:
    """One company and its products, from its index alone.

    Seeded per index (``stable_seed(seed, index)``) rather than from one
    sequential RNG, so **any range of companies can be built independently** — a
    worker builds ``[start, end)`` without first building everything before it.
    That is what lets a catalogue build shard across tasks, exactly as document
    generation seeds each document from ``(run_id, index)``.

    Market and business type are a function of the index (round-robin), so every
    locale is represented in proportion no matter how the range is sliced. The
    **sub-category** (the coherent product family) is a separate hash of the index
    so it does not line up with market or type — a retail company is one specific
    kind of shop, and its whole catalogue is that one line.
    """
    rng = Random(stable_seed(str(seed), index))
    juris, locale, currency = _MARKETS[index % len(_MARKETS)]
    business_type = business_types[index % len(business_types)]
    # Independent of the main rng stream and of the index%len alignments, so the
    # family choice does not correlate with jurisdiction or business type.
    subcats = _SUBCATEGORIES[business_type]
    category = subcats[stable_seed(f"{seed}:category", index) % len(subcats)]
    name = f"{rng.choice(_STEM_A)}{rng.choice(_STEM_B)} {rng.choice(_SUFFIX[juris])}"
    row = CompanyRow(
        company_id=f"c{index:06d}",
        name=name,
        business_type=business_type,
        product_category=category,
        jurisdiction=juris,
        locale=locale,
        currency=currency,
        # A long tail: most companies issue a little, a few issue a lot.
        weight=round(rng.paretovariate(1.5), 3),
    )
    return row, generate_products(rng, category, business_type, products_per_company)


def generate_company_range(
    start: int,
    end: int,
    *,
    seed: int = 0,
    products_per_company: int = 300,
    business_types: tuple[BusinessType, ...] = BUSINESS_TYPES,
) -> tuple[list[CompanyRow], dict[str, list[ProductTemplate]]]:
    """Companies ``[start, end)`` and their products — one shard's worth."""
    rows: list[CompanyRow] = []
    products: dict[str, list[ProductTemplate]] = {}
    for i in range(start, end):
        row, prods = generate_company(
            i, seed=seed, products_per_company=products_per_company,
            business_types=business_types,
        )
        rows.append(row)
        products[row.company_id] = prods
    return rows, products


def generate_catalogue(
    *,
    companies: int = 1_000,
    products_per_company: int = 300,
    seed: int = 0,
    business_types: tuple[BusinessType, ...] = BUSINESS_TYPES,
) -> tuple[list[CompanyRow], dict[str, list[ProductTemplate]]]:
    """Build a whole catalogue: a roster and each company's own product pool.

    Deterministic from ``seed``. Now a thin wrapper over the range builder, so
    the single-process build and a sharded one produce identical companies for
    identical indices.
    """
    if companies < 1:
        raise ValueError("a catalogue needs at least one company")
    return generate_company_range(
        0, companies, seed=seed, products_per_company=products_per_company,
        business_types=business_types,
    )
