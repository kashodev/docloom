"""Rendering tests.

These assert on the rendered HTML rather than a PDF, so they run without a
browser. PDF-specific behaviour (page counts, break placement) belongs in a
separate suite that does drive Chromium.

The locale assertions matter more than they look: fr-CA and fr-FR must not be
able to leak into one another. A Quebec invoice printing "Remise" or a French
invoice printing "TPS" would be a realistic-looking document that is wrong in a
way no downstream test would catch.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from jinja2 import UndefinedError

from docloom.core import (
    Currency,
    Jurisdiction,
    Locale,
    get_pack,
    render_record,
    render_template,
)
from docloom.packs.invoice import (
    LineItem,
    TaxRegistration,
    body_classes,
    build_context,
    column_headers,
    group_line_items,
)
from tests.factories import (
    france_tax,
    invoice,
    profile,
    quebec_tax,
    simple_lines,
    telecom_lines,
    tiered_line,
)

PACK = get_pack("invoice")


def html_for(inv) -> str:
    return render_record(PACK, inv)


# ─────────────────────────────────────────────────────────────────────────────
# Structure
# ─────────────────────────────────────────────────────────────────────────────

def test_renders_wellformed_document() -> None:
    out = html_for(invoice(simple_lines()))
    assert out.startswith("<!doctype html>")
    assert "<table class=\"items\">" in out
    assert out.count("<tbody>") >= 1
    assert "</html>" in out


def test_line_content_appears() -> None:
    out = html_for(invoice(simple_lines()))
    assert "Stainless steel hex bolt, M8 x 40mm" in out
    assert "HB-M8-40-SS" in out
    assert "$105.00" in out


def test_repeating_table_header_is_enabled() -> None:
    """Long invoices depend on the head repeating across page breaks."""
    out = html_for(invoice(simple_lines()))
    assert "table.items thead { display: table-header-group; }" in out
    assert "break-inside: avoid" in out


def test_body_classes_encode_the_variation_matrix() -> None:
    inv = invoice(
        simple_lines(),
        render_profile=profile(
            meta_position="right-rail", totals_style="boxed",
            table_style="zebra", has_logo=False,
        ),
    )
    classes = body_classes(inv)
    assert "meta-right-rail" in classes
    assert "totals-boxed" in classes
    assert "table-zebra" in classes
    assert "no-logo" in classes
    assert f'class="{classes}"' in html_for(inv)


def test_missing_context_key_raises() -> None:
    """StrictUndefined: a template typo must fail loudly, not render blank."""
    ctx = build_context(invoice(simple_lines()))
    del ctx["cols"]
    with pytest.raises(UndefinedError):
        render_template(PACK.template_root, "meta-sidebar-01", ctx)


def test_description_is_escaped() -> None:
    """Catalogue text is LLM-generated and therefore untrusted here — a stray
    angle bracket must not be able to break the table structure."""
    line = LineItem(line_no=1, description='Widget <script>alert("x")</script>',
                    quantity=D(1), unit_price=D("10.00"), extended_amount=D("10.00"))
    out = html_for(invoice([line]))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ─────────────────────────────────────────────────────────────────────────────
# Column vocabularies
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("vocab", "expected"),
    [
        ("standard", "Unit Price"),
        ("caps", "UNIT PRICE"),
        ("unit_cost", "Unit Cost"),
        ("item", "Item Description"),
        ("product", "Rate"),
    ],
)
def test_column_vocabulary_changes_headers(vocab: str, expected: str) -> None:
    inv = invoice(simple_lines(), render_profile=profile(column_vocabulary=vocab))
    assert expected in html_for(inv)


def test_unknown_vocabulary_falls_back_to_standard() -> None:
    inv = invoice(simple_lines(), render_profile=profile(column_vocabulary="nonexistent"))
    assert column_headers(inv)["description"] == "Description"


def test_part_number_gets_its_own_column_when_vocabulary_has_one() -> None:
    inv = invoice(simple_lines(), render_profile=profile(column_vocabulary="partno_first"))
    out = html_for(inv)
    assert "Item No." in out
    assert "HB-M8-40-SS" in out
    assert 'class="inline-code"' not in out


def test_part_number_is_prefixed_when_vocabulary_lacks_a_column() -> None:
    """The identifier must stay visible to an extractor even when the chosen
    vocabulary has nowhere to put it."""
    inv = invoice(simple_lines(), render_profile=profile(column_vocabulary="standard"))
    out = html_for(inv)
    assert "Item No." not in out
    assert 'class="inline-code"' in out
    assert "HB-M8-40-SS" in out


def test_closing_label_is_total_without_a_deposit() -> None:
    out = html_for(invoice(simple_lines()))
    assert ">Total<" in out
    assert "Balance Due" not in out


def test_closing_label_is_balance_due_with_a_deposit() -> None:
    """Every source template printing "Less Deposit" closes with "Balance Due"."""
    lines = simple_lines()
    inv = invoice(lines)
    inv = inv.model_copy(
        update={
            "totals": inv.totals.model_copy(
                update={"deposit": D("50.00"), "grand_total": D("160.00")}
            )
        }
    )
    out = html_for(inv)
    assert "Less Deposit" in out
    assert "Balance Due" in out


def test_minimal_vocabulary_drops_quantity_and_price() -> None:
    inv = invoice(simple_lines(), render_profile=profile(column_vocabulary="minimal"))
    out = html_for(inv)
    assert "Item Name" in out
    assert "Unit Price" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Localisation — fr-CA and fr-FR must stay distinct
# ─────────────────────────────────────────────────────────────────────────────

def test_quebec_french_uses_quebec_conventions() -> None:
    lines = simple_lines()
    base = D("210.00")
    inv = invoice(
        lines, locale=Locale.FR_CA, jurisdiction=Jurisdiction.CA_QC,
        currency=Currency.CAD, tax_buckets=quebec_tax(base),
    )
    out = html_for(inv)
    assert "TPS" in out and "TVQ" in out
    assert "Sous-total" in out
    assert "Total général" in out
    # France-only vocabulary must not leak in.
    assert "Remise" not in out
    assert "Total TTC" not in out
    assert "Total HT" not in out


def test_france_french_uses_france_conventions() -> None:
    lines = simple_lines()
    inv = invoice(
        lines, locale=Locale.FR_FR, jurisdiction=Jurisdiction.FR,
        currency=Currency.EUR, tax_buckets=france_tax(D("210.00")),
        registrations=(
            TaxRegistration(kind="SIRET", value="81234567800019"),
            TaxRegistration(kind="TVA_INTRA", value="FR40812345678"),
        ),
    )
    out = html_for(inv)
    assert "Désignation" in out
    assert "Total HT" in out
    assert "Total TTC" in out
    assert "TVA" in out
    # Quebec-only vocabulary must not leak in.
    assert "TPS" not in out
    assert "Rabais" not in out
    # Legally mandatory on a French invoice.
    assert "pénalité" in out.lower()
    assert "81234567800019" in out
    assert "FR40812345678" in out


def test_currency_formatting_per_locale() -> None:
    en = html_for(invoice(simple_lines()))
    assert "$105.00" in en

    fr = html_for(invoice(simple_lines(), locale=Locale.FR_FR,
                          jurisdiction=Jurisdiction.FR, currency=Currency.EUR))
    # Comma decimal, symbol trailing, narrow no-break space before it.
    assert "105,00\u202f€" in fr
    assert "$105.00" not in fr


def test_date_formatting_per_locale() -> None:
    assert "07/15/2026" in html_for(invoice(simple_lines()))
    assert "15 juillet 2026" in html_for(
        invoice(simple_lines(), locale=Locale.FR_CA,
                jurisdiction=Jurisdiction.CA_QC, currency=Currency.CAD)
    )
    assert "15/07/2026" in html_for(
        invoice(simple_lines(), locale=Locale.FR_FR,
                jurisdiction=Jurisdiction.FR, currency=Currency.EUR)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tiered pricing
# ─────────────────────────────────────────────────────────────────────────────

def test_tier_bands_render_as_sub_rows() -> None:
    out = html_for(invoice([tiered_line()]))
    assert out.count('class="tier"') == 3
    assert "$20.00" in out and "$135.00" in out and "$50.00" in out
    assert "$205.00" in out          # parent line total
    assert "100,001+" in out or "100001+" in out


# ─────────────────────────────────────────────────────────────────────────────
# Grouping / telecom archetype
# ─────────────────────────────────────────────────────────────────────────────

def test_grouping_preserves_order_and_subtotals() -> None:
    groups = group_line_items(tuple(telecom_lines()))
    assert [g["key"] for g in groups] == ["(416) 555-0142", "(416) 555-0188"]
    for g in groups:
        assert [s["name"] for s in g["sections"]] == ["Monthly charges", "Packet data"]
        assert g["subtotal"] == D("67.50")
        assert g["sections"][0]["subtotal"] == D("65.00")


def test_flat_invoice_groups_into_single_unnamed_group() -> None:
    """Flat and hierarchical invoices share one template code path."""
    groups = group_line_items(tuple(simple_lines()))
    assert len(groups) == 1
    assert groups[0]["key"] is None


def test_telecom_archetype_renders_hierarchy() -> None:
    inv = invoice(
        telecom_lines(),
        render_profile=profile(archetype="telecom-itemized-37"),
    )
    out = html_for(inv)
    assert "(416) 555-0142" in out
    assert "(416) 555-0188" in out
    assert "Monthly charges" in out and "Packet data" in out
    # One table per section, so each section's header repeats across breaks.
    assert out.count('<table class="items">') >= 4
    assert 'class="contd"' in out       # placeholder the PDF pass fills in
    assert "Brwsr" in out


def test_running_header_template_is_emitted() -> None:
    """Extracted by the PDF renderer for Chromium's headerTemplate."""
    out = html_for(invoice(simple_lines()))
    assert 'id="running-header"' in out
    assert "INV-2026-0042" in out
