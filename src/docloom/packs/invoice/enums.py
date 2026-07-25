"""Invoice vocabularies.

Owned by the invoice pack, not the kernel: billing models and line-item kinds
mean nothing to a contract or a delivery note. A pack is free to define
whatever closed vocabularies its record shape needs.

Every value appears verbatim in the exported golden tables, so renaming a
member is a breaking change to already-exported runs. Add members; do not
rename existing ones.
"""

from __future__ import annotations

from enum import StrEnum


class BusinessType(StrEnum):
    """What kind of business issued the invoice.

    Drives which item pools, billing models, and line-item kinds the sampler
    draws from. Deliberately decoupled from the template archetype: any
    business type may be rendered by any layout, because in reality a layout
    carries no information about the issuer's industry.
    """

    # Goods
    RETAIL = "retail"
    ECOMMERCE = "ecommerce"
    GROCERY = "grocery"
    WHOLESALE = "wholesale"
    MANUFACTURING = "manufacturing"
    PHARMACY = "pharmacy"

    # Hospitality & food
    RESTAURANT = "restaurant"
    HOSPITALITY = "hospitality"
    EVENT_SERVICES = "event_services"

    # Repair & trades
    AUTO_REPAIR = "auto_repair"
    HOME_SERVICES = "home_services"
    CONSTRUCTION = "construction"
    EQUIPMENT_RENTAL = "equipment_rental"
    FACILITIES = "facilities"

    # Health
    MEDICAL_CLINIC = "medical_clinic"
    DENTAL = "dental"
    VETERINARY = "veterinary"
    THERAPY = "therapy"

    # Professional services
    LEGAL = "legal"
    ACCOUNTING = "accounting"
    CONSULTING = "consulting"
    MARKETING_AGENCY = "marketing_agency"
    STAFFING = "staffing"
    EDUCATION = "education"

    # Technology
    B2B_SAAS = "b2b_saas"
    AI_PLATFORM = "ai_platform"
    CLOUD_INFRA = "cloud_infra"
    TELECOM = "telecom"
    UTILITIES = "utilities"

    # Financial
    INSURANCE = "insurance"
    LENDING = "lending"
    WEALTH_MANAGEMENT = "wealth_management"
    PAYMENTS = "payments"

    # Logistics
    LOGISTICS = "logistics"
    FREIGHT = "freight"

    # Consumer subscription
    FITNESS = "fitness"
    MEDIA_SUBSCRIPTION = "media_subscription"


class BillingModel(StrEnum):
    """How a line item's amount is derived.

    GRADUATED_TIER and VOLUME_TIER are genuinely different and both occur in
    the wild: graduated prices each band's units at that band's rate (like tax
    brackets), volume prices *all* units at the rate of the band reached. They
    produce different totals from identical inputs, which makes them a sharp
    test of whether an extractor understood the tier table or just read the
    bottom line.
    """

    FLAT_RATE = "flat_rate"
    PER_UNIT = "per_unit"
    GRADUATED_TIER = "graduated_tier"
    VOLUME_TIER = "volume_tier"
    METERED_USAGE = "metered_usage"
    SUBSCRIPTION = "subscription"
    SEAT_BASED = "seat_based"
    HOURLY_LABOUR = "hourly_labour"
    MILESTONE = "milestone"
    INTEREST = "interest"
    FEE_SCHEDULE = "fee_schedule"
    PREPAID_CREDIT = "prepaid_credit"


class LineItemKind(StrEnum):
    """What the row represents. Lets a single invoice mix a recurring
    subscription, metered overage, one-time fees, and a credit — as real SaaS
    and telecom invoices do."""

    PRODUCT = "product"
    SERVICE = "service"
    LABOUR = "labour"
    PART = "part"
    SUBSCRIPTION = "subscription"
    USAGE = "usage"
    SEAT = "seat"
    OVERAGE = "overage"
    FEE = "fee"
    SURCHARGE = "surcharge"
    SHIPPING = "shipping"
    ADJUSTMENT = "adjustment"
    CREDIT = "credit"
    INTEREST = "interest"
    DEPOSIT_APPLIED = "deposit_applied"


class CodeSystem(StrEnum):
    """Which coding scheme ``LineItem.code`` belongs to.

    Generalises beyond sku/part_number so medical (CPT/HCPCS), pharmacy (NDC),
    and wholesale (UNSPSC) invoices carry codes in the shape an extractor will
    actually encounter.
    """

    NONE = "none"
    SKU = "sku"
    UPC = "upc"
    EAN = "ean"
    MPN = "mpn"
    UNSPSC = "unspsc"
    CPT = "cpt"
    HCPCS = "hcpcs"
    ICD10 = "icd10"
    NDC = "ndc"
    GL_ACCOUNT = "gl_account"
    CUSTOM = "custom"


class UsageUnit(StrEnum):
    """Unit for metered line items. NONE for non-metered rows."""

    NONE = "none"

    # Time
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    MONTHS = "months"

    # Data
    MEGABYTES = "megabytes"
    GIGABYTES = "gigabytes"
    TERABYTES = "terabytes"
    GB_MONTH = "gb_month"

    # Messaging
    MESSAGES = "messages"
    EMAILS = "emails"

    # API / AI platform
    API_CALLS = "api_calls"
    REQUESTS = "requests"
    TOKENS_INPUT = "tokens_input"
    TOKENS_OUTPUT = "tokens_output"
    TOKENS_CACHED = "tokens_cached"
    IMAGES = "images"
    INFERENCES = "inferences"

    # Compute
    COMPUTE_HOURS = "compute_hours"
    GPU_HOURS = "gpu_hours"
    BUILD_MINUTES = "build_minutes"

    # Licensing
    SEATS = "seats"
    USERS = "users"
    DEVICES = "devices"

    # Commerce / utility
    TRANSACTIONS = "transactions"
    KWH = "kwh"
    LITRES = "litres"
    GALLONS = "gallons"
    MILES = "miles"
    KILOMETRES = "kilometres"
    PAGES = "pages"
    UNITS = "units"


class DiscountScheme(StrEnum):
    """How a discount is expressed on the document.

    Distinct from :class:`BillingModel` tiering — a discount reduces a price
    that has already been derived, whereas a pricing tier *is* the derivation.
    """

    NONE = "none"
    LINE_PERCENT = "line_percent"
    LINE_AMOUNT = "line_amount"
    SUMMARY_PERCENT = "summary_percent"
    SUMMARY_AMOUNT = "summary_amount"
    VOLUME_DISCOUNT = "volume_discount"
    PROMOTIONAL_CODE = "promotional_code"


class DiscountTiming(StrEnum):
    """Whether the discount is applied before or after tax is computed.

    Real invoices do both. Getting this wrong changes the grand total, so it is
    recorded explicitly rather than inferred.
    """

    PRE_TAX = "pre_tax"
    POST_TAX = "post_tax"


#: Line-item kinds that are **physical goods** — something a person can take
#: delivery of and sign for. Services, subscriptions, usage and fees are not:
#: nobody signs a delivery note for a month of consulting.
GOODS_KINDS: frozenset[LineItemKind] = frozenset({
    LineItemKind.PRODUCT,
    LineItemKind.PART,
})

#: Business types that ship physical goods. The goods-receipt variant draws only
#: from these, so the document it produces is one that would plausibly be signed
#: for on delivery.
GOODS_BUSINESS_TYPES: frozenset[BusinessType] = frozenset({
    BusinessType.RETAIL,
    BusinessType.ECOMMERCE,
    BusinessType.GROCERY,
    BusinessType.WHOLESALE,
    BusinessType.MANUFACTURING,
    BusinessType.PHARMACY,
})

#: Born-digital business types: they exist only in the software era and deliver
#: invoices as digital PDFs, so they never produced a hand-filled invoice or one
#: old enough to survive as a degraded scan. Excluded from every non-CLEAN capture
#: condition (see ``composition.resolve``). Distinct from the telecom handwritten
#: rule, which is about line count, not age. Add CLOUD_INFRA / PAYMENTS /
#: MEDIA_SUBSCRIPTION here too if they are ever wired into the catalogue roster.
DIGITAL_NATIVE_BUSINESS_TYPES: frozenset[BusinessType] = frozenset({
    BusinessType.B2B_SAAS,
    BusinessType.AI_PLATFORM,
})
