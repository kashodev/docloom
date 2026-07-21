"""Kernel contract tests.

These are the tests that make the pack architecture real. They assert the
boundary rather than any invoice behaviour: a pack declares its tables, produces
exactly those tables, supplies the three keys the kernel's filters depend on,
and is reachable through the registry. A second pack — contracts, delivery
notes — passes or fails these unchanged.
"""

from __future__ import annotations

import pytest

from docloom.core import DocumentPack, GoldenRecord, available_packs, get_pack, render_record
from docloom.core.pack import RunningHeader
from docloom.core.registry import register_pack
from docloom.packs.invoice import GoldenInvoice, InvoicePack
from tests.factories import invoice, simple_lines, tiered_line
from docloom.core import Currency, Jurisdiction, Locale

PACK = get_pack("invoice")


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

def test_invoice_pack_is_registered() -> None:
    assert "invoice" in available_packs()
    assert get_pack("invoice").name == "invoice"


def test_unknown_pack_names_what_is_available() -> None:
    with pytest.raises(KeyError, match="available: invoice"):
        get_pack("contract")


def test_registering_the_same_pack_twice_is_idempotent() -> None:
    register_pack(PACK)
    assert get_pack("invoice") is PACK


def test_registering_a_different_pack_under_a_taken_name_raises() -> None:
    """Silent shadowing would make a mis-installed plugin very hard to diagnose."""

    class Impostor(InvoicePack):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register_pack(Impostor())


# ─────────────────────────────────────────────────────────────────────────────
# Protocol conformance
# ─────────────────────────────────────────────────────────────────────────────

def test_pack_satisfies_the_protocol() -> None:
    assert isinstance(PACK, DocumentPack)


def test_record_satisfies_the_protocol() -> None:
    assert isinstance(invoice(simple_lines()), GoldenRecord)


def test_pack_template_root_exists_and_holds_archetypes() -> None:
    root = PACK.template_root
    assert root.is_dir()
    assert (root / "archetypes").is_dir()
    assert list((root / "archetypes").glob("*.html.j2"))


# ─────────────────────────────────────────────────────────────────────────────
# The record → tables contract
# ─────────────────────────────────────────────────────────────────────────────

def test_to_rows_produces_exactly_the_declared_tables() -> None:
    """A sink creates destinations from ``table_names`` before the run starts,
    so the declaration and the output must not drift."""
    rows = invoice(simple_lines()).to_rows()
    assert set(rows) == set(PACK.table_names)


def test_to_rows_row_counts_match_the_printed_document() -> None:
    inv = invoice([tiered_line()])
    rows = inv.to_rows()
    assert len(rows["invoices"]) == 1
    assert len(rows["line_items"]) == inv.printed_row_count == 4


def test_every_table_has_a_stable_column_set() -> None:
    """Parquet needs one schema per table; ragged rows would fail at export."""
    rows = invoice([tiered_line(), *simple_lines()]).to_rows()
    for table, table_rows in rows.items():
        reference = set(table_rows[0])
        for row in table_rows[1:]:
            assert set(row) == reference, f"{table}: column drift {reference ^ set(row)}"


def test_tables_declaration_lives_on_the_record() -> None:
    assert GoldenInvoice.TABLES == ("invoices", "line_items")


# ─────────────────────────────────────────────────────────────────────────────
# The pack → context contract
# ─────────────────────────────────────────────────────────────────────────────

def test_context_supplies_what_the_kernel_filters_need() -> None:
    """``| money``, ``| date`` and ``| label`` read these three keys straight
    off the context. A pack that omits one fails at render time, not import."""
    ctx = PACK.build_context(invoice(simple_lines()))
    for key in ("locale", "currency", "labels", "language"):
        assert key in ctx, f"context is missing {key!r}"


def test_labels_registry_is_reachable_from_the_pack() -> None:
    PACK.labels.validate()
    assert PACK.labels.get(PACK.labels.languages[0], "invoice")


def test_archetype_selection_comes_from_the_record() -> None:
    inv = invoice(simple_lines())
    assert PACK.archetype_for(inv) == inv.render_profile.archetype


# ─────────────────────────────────────────────────────────────────────────────
# Running header — composed by the pack, not the renderer
# ─────────────────────────────────────────────────────────────────────────────

def test_header_fields_composes_issuer_and_number() -> None:
    header = PACK.header_fields(invoice(simple_lines()))
    assert isinstance(header, RunningHeader)
    assert "Northwind Supply" in header.primary
    assert "INV-2026-0042" in header.primary
    # The page-label template carries placeholders for the renderer to fill.
    assert "{page}" in header.page_label and "{pages}" in header.page_label


def test_header_fields_is_localised_by_the_pack() -> None:
    fr = invoice(simple_lines(), locale=Locale.FR_FR,
                 jurisdiction=Jurisdiction.FR, currency=Currency.EUR)
    header = PACK.header_fields(fr)
    assert "sur" in header.page_label          # fr-FR: "Page {page} sur {pages}"
    assert "Facture" in header.primary          # fr-FR invoice-number label


def test_render_record_goes_end_to_end_through_the_pack() -> None:
    out = render_record(PACK, invoice(simple_lines()))
    assert out.startswith("<!doctype html>")
    assert "Northwind Supply" in out
