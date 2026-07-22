"""Printed label dictionaries.

Hand-authored, not LLM-generated. These are a closed vocabulary of ~60 terms
where correctness matters more than variety — a mistranslated tax label would
be indistinguishable from an extraction failure during evaluation.

Three dictionaries, and fr-CA is NOT a dialect tweak of fr-FR:

  * ``Rabais`` (QC) vs ``Remise`` (FR) — different words for a discount.
  * ``TPS``/``TVQ`` (QC) vs ``TVA`` (FR) — unrelated tax systems.
  * ``Sous-total``/``Total général`` (QC) vs ``Total HT``/``Total TTC`` (FR) —
    a structural difference, not a lexical one. A French invoice states amounts
    excluding tax (HT) then including tax (TTC); a Quebec invoice states a
    subtotal then adds tax lines. The totals block is laid out differently.
  * ``Courriel``/``Télécopie`` (QC) vs ``E-mail``/``Fax`` (FR).

Column-header variants are provided per language so the sampler can vary
vocabulary the way real ERP systems do.
"""

from __future__ import annotations

from docloom.core.locale.enums import Language
from docloom.core.locale.labels import LabelRegistry

LABELS: dict[Language, dict[str, str]] = {
    # ─────────────────────────────────────────────────────────────────────────
    Language.EN: {
        "invoice": "Invoice",
        "invoice_number": "Invoice No.",
        "issue_date": "Date",
        "due_date": "Due",
        "billing_date": "Billing Date",
        "service_date": "Service Date",
        "customer_id": "Customer ID",
        "purchase_order": "PO Number",
        "bill_to": "Bill To",
        "issued_by": "From",
        "description": "Description",
        "reference": "Reference",
        "part_number": "Item No.",
        "quantity": "Quantity",
        "unit_price": "Unit Price",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "discount": "Discount",
        "shipping": "Shipping & Handling",
        "tax_sales": "Sales Tax",
        "tax_gst": "GST",
        "tax_qst": "QST",
        "tax_pst": "PST",
        "tax_hst": "HST",
        "tax_vat": "VAT",
        "tax_tva": "VAT",
        "tax_total": "Total Tax",
        "deposit": "Less Deposit",
        "total": "Total",
        "balance_due": "Balance Due",
        "payment_terms": "Payment Terms",
        "registrations": "Registration Numbers",
        "notes": "Notes",
        "thank_you": "Thank you for your business!",
        "page_of": "Page {page} of {pages}",
        "continued": "(cont'd)",
        "authorised_signature": "Authorised Signature",
        "total_before_tax": "Total before taxes",
        "late_penalty": "Late payment charges apply.",
    },
    # ─────────────────────────────────────────────────────────────────────────
    Language.FR_CA: {
        "invoice": "Facture",
        "invoice_number": "Numéro de la facture",
        "issue_date": "Date de facturation",
        "due_date": "Date d'échéance",
        "billing_date": "Date de facturation",
        "service_date": "Date du service",
        "customer_id": "Numéro de client",
        "purchase_order": "Votre bon de commande",
        "bill_to": "Client",
        "issued_by": "Émis par",
        "description": "Description",
        "reference": "Référence",
        "part_number": "Nº d'article",
        "quantity": "Quantité",
        "unit_price": "Prix unitaire",
        "amount": "Montant",
        "subtotal": "Sous-total",
        "discount": "Rabais",
        "shipping": "Frais de livraison",
        "tax_sales": "Taxe de vente",
        "tax_gst": "TPS",
        "tax_qst": "TVQ",
        "tax_pst": "TVP",
        "tax_hst": "TVH",
        "tax_vat": "TVA",
        "tax_tva": "TVA",
        "tax_total": "Total des taxes",
        "deposit": "Moins l'acompte",
        "total": "Total général",
        "balance_due": "Solde dû",
        "payment_terms": "Modalités de paiement",
        "registrations": "Numéros d'enregistrement",
        "notes": "Notes",
        "thank_you": "Merci de votre confiance!",
        "page_of": "Page {page} de {pages}",
        "continued": "(suite)",
        "authorised_signature": "Signature autorisée",
        "total_before_tax": "Total avant taxes",
        "late_penalty": "Des frais supplémentaires s'appliquent après l'échéance.",
    },
    # ─────────────────────────────────────────────────────────────────────────
    Language.FR_FR: {
        "invoice": "Facture",
        "invoice_number": "Facture n°",
        "issue_date": "Date de facture",
        "due_date": "Date d'échéance",
        "billing_date": "Date de facturation",
        "service_date": "Date d'exécution",
        "customer_id": "Code client",
        "purchase_order": "Bon de commande",
        "bill_to": "Facturé à",
        "issued_by": "Émetteur",
        "description": "Désignation",
        "reference": "Référence",
        "part_number": "Réf.",
        "quantity": "Quantité",
        "unit_price": "Prix unitaire HT",
        "amount": "Montant HT",
        "subtotal": "Total HT",
        "discount": "Remise",
        "shipping": "Frais de port",
        "tax_sales": "TVA",
        "tax_gst": "TVA",
        "tax_qst": "TVA",
        "tax_pst": "TVA",
        "tax_hst": "TVA",
        "tax_vat": "TVA",
        "tax_tva": "TVA",
        "tax_total": "Total TVA",
        "deposit": "Acompte versé",
        "total": "Total TTC",
        "balance_due": "Net à payer",
        "payment_terms": "Conditions de règlement",
        "registrations": "Mentions légales",
        "notes": "Observations",
        "thank_you": "Nous vous remercions de votre confiance.",
        "page_of": "Page {page} sur {pages}",
        "continued": "(suite)",
        "authorised_signature": "Signature autorisée",
        "total_before_tax": "Total HT",
        # Legally mandatory on a French invoice (Code de commerce L441-10).
        "late_penalty": (
            "En cas de retard de paiement, une pénalité de 3 fois le taux d'intérêt légal "
            "sera appliquée, ainsi qu'une indemnité forfaitaire pour frais de recouvrement "
            "de 40 €."
        ),
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Column-header vocabularies — sampled per company so different "ERP systems"
# label the same concepts differently, as they do in reality.
# ─────────────────────────────────────────────────────────────────────────────
COLUMN_VOCABULARIES: dict[Language, dict[str, dict[str, str]]] = {
    Language.EN: {
        "standard": {
            "description": "Description",
            "quantity": "Quantity",
            "unit_price": "Unit Price",
            "amount": "Amount",
        },
        "caps": {
            "description": "DESCRIPTION",
            "quantity": "QUANTITY",
            "unit_price": "UNIT PRICE",
            "amount": "AMOUNT",
        },
        "unit_cost": {
            "description": "Description",
            "quantity": "Qty",
            "unit_price": "Unit Cost",
            "amount": "Amount",
        },
        "item": {
            "description": "Item Description",
            "quantity": "QTY",
            "unit_price": "Price",
            "amount": "Amount",
        },
        "product": {
            "description": "Product",
            "quantity": "Qty",
            "unit_price": "Rate",
            "amount": "Total",
        },
        "partno_first": {
            "part_number": "Item No.",
            "description": "Description",
            "quantity": "QTY",
            "unit_price": "Cost",
            "amount": "Amount",
        },
        "minimal": {
            "description": "Item Name",
            "amount": "Cost",
        },
        "no_unit_price": {
            "description": "Description",
            "quantity": "Quantity",
            "amount": "Amount",
        },
    },
    Language.FR_CA: {
        "standard": {
            "description": "Description",
            "quantity": "Quantité",
            "unit_price": "Prix unitaire",
            "amount": "Montant",
        },
        "titre": {
            "reference": "Référence",
            "quantity": "Quantité",
            "description": "Désignation",
            "unit_price": "Prix unitaire",
            "amount": "Montant",
        },
        "prix_unite": {
            "description": "Description",
            "quantity": "Quantité",
            "unit_price": "Prix d'unité",
            "amount": "Total",
        },
        "minimal": {
            "description": "Description",
            "amount": "Montant",
        },
    },
    Language.FR_FR: {
        "standard": {
            "description": "Désignation",
            "quantity": "Quantité",
            "unit_price": "Prix unitaire HT",
            "amount": "Montant HT",
        },
        "reference": {
            "reference": "Référence",
            "description": "Désignation",
            "quantity": "Qté",
            "unit_price": "P.U. HT",
            "amount": "Montant HT",
        },
        "with_vat_col": {
            "description": "Désignation",
            "quantity": "Qté",
            "unit_price": "P.U. HT",
            "tax_rate": "TVA",
            "amount": "Montant HT",
        },
        "minimal": {
            "description": "Désignation",
            "amount": "Montant",
        },
    },
}


#: The invoice pack's printed vocabulary. The kernel's registry supplies lookup
#: and the completeness check; the words above are all this pack contributes.
LABEL_REGISTRY = LabelRegistry(
    labels=LABELS,
    vocabularies=COLUMN_VOCABULARIES,
    reference_language=Language.EN,
)
