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
CAT_GARDEN = "garden and outdoor"
CAT_PET = "pet supplies"
CAT_SPORTS = "sporting goods"
CAT_BEAUTY = "health and beauty"
CAT_TOYS = "toys and games"
# Service families — the "product" is a professional service, billed as SERVICE
# / FLAT_RATE like accounting, so a line reads "Commercial contract drafting,
# fixed fee (engagement)" rather than a stocked good.
CAT_LEGAL = "legal services"
CAT_CONSULTING = "consulting services"
CAT_MARKETING = "marketing services"

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
    CAT_GARDEN: {
        "material": ("Terracotta", "Galvanised", "Plastic", "Cedar", "Powder-coated",
                     "Teak", "Stone", "Rattan"),
        "form": ("plant pot", "garden hose", "watering can", "hand trowel",
                 "planter box", "pruning shears", "garden fork", "trellis",
                 "compost bin", "raised bed kit"),
        "variant": ("small", "medium", "large", "green", "40cm", "60cm"),
        "pack": ("each", "set", "pack of 2", "set of 3"),
    },
    CAT_PET: {
        "material": ("Grain-free", "Stainless steel", "Ceramic", "Nylon",
                     "Fleece-lined", "Rubber", "Rope"),
        "form": ("dog food", "cat litter", "chew toy", "pet bed", "food bowl",
                 "collar", "leash", "scratching post", "grooming brush",
                 "carrier crate"),
        "variant": ("small", "medium", "large", "2kg", "5kg", "adult"),
        "pack": ("each", "bag", "pack of 2", "pack of 6"),
    },
    CAT_SPORTS: {
        "material": ("Neoprene", "Rubber", "Foam", "Carbon", "Leather", "Mesh",
                     "Aluminium"),
        "form": ("yoga mat", "dumbbell set", "resistance band", "water bottle",
                 "tennis racket", "football", "jump rope", "gym gloves",
                 "foam roller", "kettlebell"),
        "variant": ("small", "medium", "large", "black", "5kg", "10kg"),
        "pack": ("each", "pair", "set", "set of 2"),
    },
    CAT_BEAUTY: {
        "material": ("Moisturising", "Exfoliating", "Fragrance-free", "Organic",
                     "Anti-ageing", "Hydrating", "Volumising"),
        "form": ("shampoo", "body lotion", "face cream", "lip balm",
                 "makeup brush set", "nail polish", "hand soap", "sunscreen",
                 "hair conditioner", "facial cleanser"),
        "variant": ("50ml", "100ml", "250ml", "travel size", "sensitive", "SPF 30"),
        "pack": ("each", "pack of 2", "set", "gift set"),
    },
    CAT_TOYS: {
        "material": ("Wooden", "Plush", "Educational", "Electronic", "Plastic",
                     "Collectible", "Classic"),
        "form": ("board game", "jigsaw puzzle", "building blocks set",
                 "action figure", "plush toy", "toy car", "card game", "art kit",
                 "remote-control car", "dollhouse"),
        "variant": ("small", "medium", "large", "age 3+", "age 6+", "2-player"),
        "pack": ("each", "set", "pack of 2", "box"),
    },
    # ── Service families ────────────────────────────────────────────────────
    # The material is an orthogonal qualifier (scope, seniority, engagement type),
    # never a repeat of the form noun, so "Commercial contract drafting" reads
    # cleanly and never "Strategic strategy consultation".
    CAT_LEGAL: {
        "material": ("Commercial", "Corporate", "Residential", "Standard",
                     "Complex", "Preliminary"),
        "form": ("contract drafting", "legal consultation", "court representation",
                 "due diligence review", "trademark filing", "will preparation",
                 "lease agreement", "compliance advice"),
        "variant": ("standard", "expedited", "fixed fee", "priority"),
        "pack": ("engagement", "hourly", "monthly retainer"),
    },
    CAT_CONSULTING: {
        "material": ("Strategic", "Operational", "Technical", "Executive",
                     "Senior", "Digital"),
        "form": ("process audit", "implementation support", "training workshop",
                 "market analysis", "change management", "feasibility study",
                 "advisory session", "roadmap review"),
        "variant": ("standard", "expedited", "on-site", "remote"),
        "pack": ("engagement", "day rate", "hourly"),
    },
    CAT_MARKETING: {
        "material": ("Managed", "Full-service", "Advanced", "Standard",
                     "Bespoke", "Ongoing"),
        "form": ("SEO campaign", "social media management", "content writing",
                 "brand identity", "email campaign", "PPC management",
                 "website design", "video production"),
        "variant": ("monthly", "one-time", "per campaign", "quarterly"),
        "pack": ("project", "monthly retainer", "package"),
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
    # Garden & outdoor, pet, sports, beauty and toys materials.
    "Terracotta": "en terre cuite", "Plastic": "en plastique", "Cedar": "en cèdre",
    "Powder-coated": "thermolaqué", "Teak": "en teck", "Stone": "en pierre",
    "Rattan": "en rotin", "Grain-free": "sans céréales", "Fleece-lined": "doublé polaire",
    "Rubber": "en caoutchouc", "Rope": "en corde", "Neoprene": "en néoprène",
    "Foam": "en mousse", "Carbon": "en carbone", "Mesh": "en maille",
    "Moisturising": "hydratant", "Exfoliating": "exfoliant", "Fragrance-free": "sans parfum",
    "Organic": "biologique", "Anti-ageing": "anti-âge", "Hydrating": "hydratant",
    "Volumising": "volumateur", "Wooden": "en bois", "Plush": "en peluche",
    "Educational": "éducatif", "Electronic": "électronique", "Collectible": "de collection",
    "Classic": "classique",
    # Service qualifiers (legal, consulting, marketing).
    "Commercial": "commerciale", "Residential": "résidentielle", "Complex": "complexe",
    "Preliminary": "préliminaire", "Strategic": "stratégique", "Operational": "opérationnel",
    "Technical": "technique", "Senior": "senior", "Digital": "numérique",
    "Managed": "géré", "Full-service": "complet", "Advanced": "avancé",
    "Bespoke": "sur mesure", "Ongoing": "continu",
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
    # Garden & outdoor.
    "plant pot": "Pot de fleurs", "garden hose": "Tuyau d'arrosage",
    "watering can": "Arrosoir", "hand trowel": "Déplantoir",
    "planter box": "Jardinière", "pruning shears": "Sécateur",
    "garden fork": "Fourche de jardin", "trellis": "Treillis",
    "compost bin": "Composteur", "raised bed kit": "Kit de carré potager",
    # Pet supplies.
    "dog food": "Nourriture pour chien", "cat litter": "Litière pour chat",
    "chew toy": "Jouet à mâcher", "pet bed": "Panier pour animal",
    "food bowl": "Gamelle", "collar": "Collier", "leash": "Laisse",
    "scratching post": "Griffoir", "grooming brush": "Brosse de toilettage",
    "carrier crate": "Caisse de transport",
    # Sporting goods.
    "yoga mat": "Tapis de yoga", "dumbbell set": "Jeu d'haltères",
    "resistance band": "Bande de résistance", "water bottle": "Gourde",
    "tennis racket": "Raquette de tennis", "football": "Ballon de football",
    "jump rope": "Corde à sauter", "gym gloves": "Gants de sport",
    "foam roller": "Rouleau de massage", "kettlebell": "Kettlebell",
    # Health & beauty.
    "shampoo": "Shampooing", "body lotion": "Lait corporel",
    "face cream": "Crème pour le visage", "lip balm": "Baume à lèvres",
    "makeup brush set": "Jeu de pinceaux de maquillage", "nail polish": "Vernis à ongles",
    "hand soap": "Savon pour les mains", "sunscreen": "Crème solaire",
    "hair conditioner": "Après-shampooing", "facial cleanser": "Nettoyant visage",
    # Toys & games.
    "board game": "Jeu de société", "jigsaw puzzle": "Casse-tête",
    "building blocks set": "Jeu de blocs de construction",
    "action figure": "Figurine d'action", "plush toy": "Peluche",
    "toy car": "Petite voiture", "card game": "Jeu de cartes",
    "art kit": "Trousse d'art", "remote-control car": "Voiture télécommandée",
    "dollhouse": "Maison de poupée",
    # Legal services.
    "contract drafting": "Rédaction de contrat", "legal consultation": "Consultation juridique",
    "court representation": "Représentation au tribunal",
    "due diligence review": "Audit de diligence raisonnable",
    "trademark filing": "Dépôt de marque", "will preparation": "Rédaction de testament",
    "lease agreement": "Contrat de bail", "compliance advice": "Conseil en conformité",
    # Consulting services.
    "process audit": "Audit de processus",
    "implementation support": "Accompagnement à la mise en œuvre",
    "training workshop": "Atelier de formation", "market analysis": "Analyse de marché",
    "change management": "Gestion du changement", "feasibility study": "Étude de faisabilité",
    "advisory session": "Séance de conseil", "roadmap review": "Revue de feuille de route",
    # Marketing services.
    "SEO campaign": "Campagne SEO", "social media management": "Gestion des réseaux sociaux",
    "content writing": "Rédaction de contenu", "brand identity": "Identité de marque",
    "email campaign": "Campagne courriel", "PPC management": "Gestion de campagne SEA",
    "website design": "Conception de site web", "video production": "Production vidéo",
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
    BusinessType.LEGAL: ("120.00", "3500.00"),
    BusinessType.CONSULTING: ("180.00", "4500.00"),
    BusinessType.MARKETING_AGENCY: ("150.00", "6000.00"),
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
    BusinessType.LEGAL: (LineItemKind.SERVICE, BillingModel.FLAT_RATE,
                         CodeSystem.NONE, UsageUnit.NONE),
    BusinessType.CONSULTING: (LineItemKind.SERVICE, BillingModel.FLAT_RATE,
                              CodeSystem.NONE, UsageUnit.NONE),
    BusinessType.MARKETING_AGENCY: (LineItemKind.SERVICE, BillingModel.FLAT_RATE,
                                    CodeSystem.NONE, UsageUnit.NONE),
}

# Which sub-categories each business type sells. A company draws exactly one, so
# its whole catalogue is one coherent line. Narrow types have a single family;
# `retail` is the broad one that otherwise drifts, so it fans out — different
# retail companies become different kinds of shop, each internally coherent.
_SUBCATEGORIES: dict[BusinessType, tuple[str, ...]] = {
    BusinessType.RETAIL: (CAT_HARDWARE, CAT_OFFICE, CAT_APPAREL, CAT_KITCHEN,
                          CAT_GARDEN, CAT_PET, CAT_SPORTS, CAT_BEAUTY, CAT_TOYS),
    BusinessType.WHOLESALE: (CAT_PACKAGING,),
    BusinessType.AUTO_REPAIR: (CAT_AUTO,),
    BusinessType.B2B_SAAS: (CAT_SAAS,),
    BusinessType.ACCOUNTING: (CAT_ACCOUNTING,),
    BusinessType.AI_PLATFORM: (CAT_AI,),
    BusinessType.TELECOM: (CAT_TELECOM,),
    BusinessType.LEGAL: (CAT_LEGAL,),
    BusinessType.CONSULTING: (CAT_CONSULTING,),
    BusinessType.MARKETING_AGENCY: (CAT_MARKETING,),
}

#: The business types a catalogue spans (the round-robin the sampler cycles).
BUSINESS_TYPES: tuple[BusinessType, ...] = tuple(_SUBCATEGORIES)

# ── LLM niches: a finer specialty layer, LLM-catalogue only ──────────────────
# A coarse family (above) is the unit the *procedural* builder can express — it
# is what the slot tables, the few-shot skeleton and the fallback are drawn from.
# The LLM needs none of that machinery to go finer, so each family fans out into
# ~10 niches: a specific kind of shop *within* the family (apparel → activewear,
# footwear, workwear …). The LLM prompt names the niche (see build_prompt), which
# multiplies the distinct kinds of company ~10x without authoring a procedural
# slot table and its French rendering for each one.
#
# Because a niche lies inside exactly one coarse family, the procedural skeleton
# and fallback for that family stay valid — a company assigned "women's
# activewear" still has an apparel skeleton to anchor and fall back to. The
# procedural / offline build ignores the niche entirely and stays at family
# granularity, so nothing new is required for offline runs.
#
# Each niche is an (English, French) pair; a company is prompted in its own
# language, so the stored label is already localised (see generate_company).
_NICHES: dict[str, tuple[tuple[str, str], ...]] = {
    CAT_HARDWARE: (
        ("threaded fasteners and fixings", "visserie et fixations filetées"),
        ("power tools and accessories", "outils électriques et accessoires"),
        ("hand tools", "outils à main"),
        ("plumbing fittings and valves", "raccords et robinetterie de plomberie"),
        ("electrical fittings and conduit", "appareillage et conduits électriques"),
        ("abrasives and cutting discs", "abrasifs et disques de coupe"),
        ("adhesives and sealants", "adhésifs et mastics"),
        ("site safety equipment", "équipements de sécurité de chantier"),
        ("wall anchors and fixings", "chevilles et fixations murales"),
        ("door and window hardware", "quincaillerie de porte et fenêtre"),
    ),
    CAT_OFFICE: (
        ("stationery and desk supplies", "papeterie et fournitures de bureau"),
        ("office furniture", "mobilier de bureau"),
        ("printers and consumables", "imprimantes et consommables"),
        ("computer peripherals", "périphériques informatiques"),
        ("networking equipment", "équipement réseau"),
        ("filing and storage", "classement et rangement"),
        ("presentation equipment", "matériel de présentation"),
        ("ergonomic accessories", "accessoires ergonomiques"),
        ("cables and adapters", "câbles et adaptateurs"),
        ("paper products", "produits papier"),
    ),
    CAT_APPAREL: (
        ("men's casual clothing", "vêtements décontractés pour hommes"),
        ("women's activewear", "vêtements de sport pour femmes"),
        ("children's clothing", "vêtements pour enfants"),
        ("outerwear and coats", "manteaux et vestes"),
        ("footwear", "chaussures"),
        ("formalwear and suits", "tenues de cérémonie et costumes"),
        ("workwear and uniforms", "vêtements de travail et uniformes"),
        ("fashion accessories", "accessoires de mode"),
        ("socks and underwear", "chaussettes et sous-vêtements"),
        ("knitwear and sweaters", "tricots et pulls"),
    ),
    CAT_KITCHEN: (
        ("cookware and pans", "batterie de cuisine et poêles"),
        ("bakeware", "moules et ustensiles de pâtisserie"),
        ("knives and cutlery", "couteaux et coutellerie"),
        ("food storage containers", "boîtes de conservation"),
        ("small kitchen appliances", "petit électroménager de cuisine"),
        ("tableware and dinnerware", "vaisselle et arts de la table"),
        ("kitchen utensils and gadgets", "ustensiles et accessoires de cuisine"),
        ("glassware and drinkware", "verrerie et verres"),
        ("home textiles and linens", "linge de maison"),
        ("home cleaning and organisation", "nettoyage et rangement de la maison"),
    ),
    CAT_PACKAGING: (
        ("corrugated boxes", "boîtes en carton ondulé"),
        ("mailing envelopes and mailers", "enveloppes et pochettes d'expédition"),
        ("protective packaging", "emballage de protection"),
        ("tapes and strapping", "rubans et cerclage"),
        ("labels and stickers", "étiquettes et autocollants"),
        ("poly bags and film", "sacs et films plastique"),
        ("void fill and cushioning", "calage et rembourrage"),
        ("pallets and crates", "palettes et caisses"),
        ("food packaging", "emballage alimentaire"),
        ("warehouse shipping supplies", "fournitures d'expédition d'entrepôt"),
    ),
    CAT_AUTO: (
        ("brake components", "composants de freinage"),
        ("engine parts", "pièces moteur"),
        ("filters and fluids", "filtres et fluides"),
        ("suspension and steering", "suspension et direction"),
        ("batteries and electrical", "batteries et électricité"),
        ("belts and hoses", "courroies et durites"),
        ("body panels and trim", "carrosserie et garnitures"),
        ("tyres and wheels", "pneus et roues"),
        ("vehicle lighting", "éclairage automobile"),
        ("workshop consumables", "consommables d'atelier"),
    ),
    CAT_SAAS: (
        ("project management software", "logiciel de gestion de projet"),
        ("CRM software", "logiciel CRM"),
        ("analytics and BI platforms", "plateformes d'analytique et BI"),
        ("team communication tools", "outils de communication d'équipe"),
        ("security and identity software", "logiciel de sécurité et d'identité"),
        ("developer tooling", "outils pour développeurs"),
        ("HR and payroll software", "logiciel RH et de paie"),
        ("marketing automation", "automatisation du marketing"),
        ("design and creative tools", "outils de conception et de création"),
        ("cloud storage and backup", "stockage et sauvegarde cloud"),
    ),
    CAT_ACCOUNTING: (
        ("bookkeeping services", "services de tenue de comptes"),
        ("tax preparation", "préparation fiscale"),
        ("payroll services", "services de paie"),
        ("audit and assurance", "audit et certification"),
        ("financial advisory", "conseil financier"),
        ("company formation", "création d'entreprise"),
        ("VAT and sales-tax filing", "déclaration de TVA"),
        ("management accounts", "comptabilité de gestion"),
        ("R&D tax credit claims", "crédit d'impôt recherche"),
        ("financial forecasting", "prévisions financières"),
    ),
    CAT_AI: (
        ("LLM text inference", "inférence de texte LLM"),
        ("image generation", "génération d'images"),
        ("speech-to-text", "transcription vocale"),
        ("text-to-speech", "synthèse vocale"),
        ("text embeddings", "plongements de texte"),
        ("model fine-tuning", "réglage fin de modèles"),
        ("vector search", "recherche vectorielle"),
        ("content moderation", "modération de contenu"),
        ("machine translation", "traduction automatique"),
        ("document extraction", "extraction documentaire"),
    ),
    CAT_TELECOM: (
        ("mobile data plans", "forfaits de données mobiles"),
        ("voice call minutes", "minutes d'appel vocal"),
        ("SMS and messaging", "SMS et messagerie"),
        ("broadband internet", "internet haut débit"),
        ("VoIP and SIP trunking", "VoIP et lignes SIP"),
        ("IoT connectivity", "connectivité IoT"),
        ("international roaming", "itinérance internationale"),
        ("cloud PBX", "standard téléphonique cloud"),
        ("dedicated leased lines", "lignes louées dédiées"),
        ("data-centre colocation", "hébergement en centre de données"),
    ),
    CAT_GARDEN: (
        ("potted plants and planters", "plantes en pot et jardinières"),
        ("garden hand tools", "outils de jardinage à main"),
        ("watering and irrigation", "arrosage et irrigation"),
        ("outdoor furniture", "mobilier de jardin"),
        ("barbecues and grills", "barbecues et grils"),
        ("lawn care", "entretien de la pelouse"),
        ("seeds and bulbs", "graines et bulbes"),
        ("fencing and trellis", "clôtures et treillis"),
        ("garden lighting", "éclairage de jardin"),
        ("composting and soil", "compostage et terreau"),
    ),
    CAT_PET: (
        ("dog food and treats", "nourriture et friandises pour chiens"),
        ("cat food and litter", "nourriture et litière pour chats"),
        ("pet toys", "jouets pour animaux"),
        ("beds and bedding", "paniers et couchages"),
        ("collars and leashes", "colliers et laisses"),
        ("grooming supplies", "produits de toilettage"),
        ("aquarium supplies", "accessoires d'aquarium"),
        ("small animal supplies", "accessoires pour petits animaux"),
        ("bowls and feeders", "gamelles et distributeurs"),
        ("pet health and hygiene", "santé et hygiène animale"),
    ),
    CAT_SPORTS: (
        ("fitness and gym equipment", "équipement de fitness et de musculation"),
        ("yoga and pilates gear", "matériel de yoga et pilates"),
        ("team sports equipment", "équipement de sports collectifs"),
        ("racket sports", "sports de raquette"),
        ("cycling accessories", "accessoires de cyclisme"),
        ("running gear", "matériel de course"),
        ("water sports equipment", "équipement de sports nautiques"),
        ("camping and hiking gear", "matériel de camping et de randonnée"),
        ("sports nutrition", "nutrition sportive"),
        ("sports activewear and footwear", "vêtements et chaussures de sport"),
    ),
    CAT_BEAUTY: (
        ("skincare", "soins de la peau"),
        ("haircare", "soins capillaires"),
        ("cosmetics and makeup", "cosmétiques et maquillage"),
        ("fragrances", "parfums"),
        ("bath and body", "bain et corps"),
        ("nail care", "soins des ongles"),
        ("men's grooming", "soins pour hommes"),
        ("sun care", "protection solaire"),
        ("vitamins and supplements", "vitamines et compléments"),
        ("oral care", "soins bucco-dentaires"),
    ),
    CAT_TOYS: (
        ("board games and puzzles", "jeux de société et casse-tête"),
        ("building toys", "jeux de construction"),
        ("dolls and figures", "poupées et figurines"),
        ("plush toys", "peluches"),
        ("educational toys", "jouets éducatifs"),
        ("outdoor toys", "jouets de plein air"),
        ("arts and crafts", "arts et loisirs créatifs"),
        ("remote-control toys", "jouets télécommandés"),
        ("card and trading games", "jeux de cartes et à collectionner"),
        ("baby and toddler toys", "jouets pour bébés et tout-petits"),
    ),
    CAT_LEGAL: (
        ("corporate and commercial law", "droit des affaires et commercial"),
        ("litigation and disputes", "contentieux et litiges"),
        ("intellectual property", "propriété intellectuelle"),
        ("real estate and conveyancing", "droit immobilier"),
        ("employment law", "droit du travail"),
        ("immigration law", "droit de l'immigration"),
        ("family law", "droit de la famille"),
        ("wills and probate", "testaments et successions"),
        ("tax law", "droit fiscal"),
        ("contract law", "droit des contrats"),
    ),
    CAT_CONSULTING: (
        ("management consulting", "conseil en management"),
        ("IT and technology consulting", "conseil en informatique et technologie"),
        ("HR consulting", "conseil en ressources humaines"),
        ("strategy consulting", "conseil en stratégie"),
        ("operations consulting", "conseil en opérations"),
        ("financial consulting", "conseil financier"),
        ("change management", "gestion du changement"),
        ("sustainability consulting", "conseil en développement durable"),
        ("data and analytics consulting", "conseil en données et analytique"),
        ("supply chain consulting", "conseil en chaîne d'approvisionnement"),
    ),
    CAT_MARKETING: (
        ("search engine optimisation", "référencement naturel (SEO)"),
        ("pay-per-click advertising", "publicité au coût par clic"),
        ("social media marketing", "marketing des réseaux sociaux"),
        ("content marketing", "marketing de contenu"),
        ("branding and design", "image de marque et design"),
        ("email marketing", "marketing par courriel"),
        ("web design and development", "conception et développement web"),
        ("public relations", "relations publiques"),
        ("video and creative production", "production vidéo et créative"),
        ("marketing analytics", "analytique marketing"),
    ),
}


def niche_space() -> int:
    """Distinct LLM niches across every family — the count the LLM prompt can name."""
    return sum(len(v) for v in _NICHES.values())


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
    # Garden, pet, sports, beauty and toys variants.
    "green": "vert", "40cm": "40 cm", "60cm": "60 cm", "2kg": "2 kg", "5kg": "5 kg",
    "adult": "adulte", "10kg": "10 kg", "50ml": "50 ml", "100ml": "100 ml",
    "250ml": "250 ml", "travel size": "format voyage", "sensitive": "peau sensible",
    "SPF 30": "FPS 30", "age 3+": "3 ans et plus", "age 6+": "6 ans et plus",
    "2-player": "2 joueurs",
    # Service variants (legal, consulting, marketing).
    "fixed fee": "forfait fixe", "priority": "prioritaire", "on-site": "sur site",
    "remote": "à distance", "one-time": "ponctuel", "per campaign": "par campagne",
    "quarterly": "trimestriel",
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
    # Garden, pet, sports, beauty and toys packs.
    "set of 3": "jeu de 3", "bag": "sachet", "pack of 6": "paquet de 6",
    "set of 2": "jeu de 2", "gift set": "coffret cadeau", "box": "boîte",
    # Service packs (legal, consulting, marketing).
    "day rate": "tarif journalier", "project": "projet", "package": "forfait",
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
    # A finer specialty inside the family, for the LLM prompt only. Hashed
    # independently again so the niche does not correlate with market, type or the
    # family draw. Stored already localised — the label the company is prompted
    # with is the one that prints, so a French company gets a French niche.
    niches = _NICHES.get(category, ())
    if niches:
        en, fr = niches[stable_seed(f"{seed}:niche", index) % len(niches)]
        llm_niche = fr if str(locale).startswith("fr") else en
    else:
        llm_niche = ""
    name = f"{rng.choice(_STEM_A)}{rng.choice(_STEM_B)} {rng.choice(_SUFFIX[juris])}"
    row = CompanyRow(
        company_id=f"c{index:06d}",
        name=name,
        business_type=business_type,
        product_category=category,
        llm_niche=llm_niche,
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
