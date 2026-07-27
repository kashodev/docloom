"""Goods-receipt variant tests.

A goods receipt is a handwritten delivery note the customer signs for on
receipt. Two things make it more than an extra block of markup:

* it is **only valid for physical goods** — nobody signs a delivery note for a
  month of consulting — which the record enforces rather than merely documents;
* the receiver is a **different person**, so the block is in a different hand
  from the issuer's signature.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

import docloom.packs  # noqa: F401 - registers the invoice pack
from docloom.core import get_pack
from docloom.core.enums import DocumentCondition
from docloom.core.render import render_record
from docloom.packs.invoice import InvoiceSampler
from docloom.packs.invoice.enums import GOODS_BUSINESS_TYPES, GOODS_KINDS, LineItemKind
from docloom.packs.invoice.handwriting import handwriting_for
from tests.factories import invoice, simple_lines

PACK = get_pack("invoice")


def goods_invoice(**kw):
    lines = tuple(li.model_copy(update={"kind": LineItemKind.PRODUCT})
                  for li in simple_lines())
    base = invoice(lines)
    return base.model_copy(update={
        "condition": DocumentCondition.HANDWRITTEN, "goods_receipt": True, **kw,
    })


# ── The physical-goods constraint ───────────────────────────────────────────
def test_a_goods_receipt_of_physical_goods_is_valid() -> None:
    inv = goods_invoice()
    assert inv.goods_receipt is True
    assert all(li.kind in GOODS_KINDS for li in inv.line_items)


@pytest.mark.parametrize("kind", [
    LineItemKind.SERVICE, LineItemKind.SUBSCRIPTION, LineItemKind.LABOUR,
    LineItemKind.USAGE, LineItemKind.FEE,
])
def test_a_goods_receipt_rejects_anything_that_is_not_deliverable(kind: LineItemKind) -> None:
    """Nobody signs a delivery note for a month of consulting."""
    lines = tuple(li.model_copy(update={"kind": kind}) for li in simple_lines())
    with pytest.raises(ValidationError, match="physical goods"):
        invoice(lines).model_copy(update={"goods_receipt": True}).model_validate(
            invoice(lines).model_copy(update={"goods_receipt": True}).model_dump()
        )


def test_a_mixed_invoice_cannot_be_a_goods_receipt() -> None:
    lines = list(simple_lines())
    mixed = (lines[0].model_copy(update={"kind": LineItemKind.PRODUCT}),
             lines[1].model_copy(update={"kind": LineItemKind.SERVICE}))
    inv = invoice(tuple(mixed))
    with pytest.raises(ValidationError, match="physical goods"):
        type(inv).model_validate({**inv.model_dump(), "goods_receipt": True})


def test_received_date_cannot_precede_the_invoice() -> None:
    inv = goods_invoice()
    with pytest.raises(ValidationError, match="precedes issue_date"):
        type(inv).model_validate(
            {**inv.model_dump(), "received_date": inv.issue_date - timedelta(days=1)}
        )


def test_a_normal_invoice_is_unaffected_by_the_validator() -> None:
    """The rule only bites when goods_receipt is set."""
    assert invoice(simple_lines()).goods_receipt is False


# ── The sampler variant ─────────────────────────────────────────────────────
def test_sampler_variant_always_produces_deliverable_goods() -> None:
    sampler = InvoiceSampler(goods_receipt=True, max_line_items=10)
    for n in range(40):
        inv = sampler.generate("gr", n)
        assert inv.goods_receipt is True
        assert inv.business_type in GOODS_BUSINESS_TYPES, inv.business_type
        assert all(li.kind in GOODS_KINDS for li in inv.line_items)
        assert inv.condition is DocumentCondition.HANDWRITTEN
        assert inv.received_date is not None and inv.received_date >= inv.issue_date


def test_sampler_default_is_not_a_goods_receipt() -> None:
    inv = InvoiceSampler(max_line_items=10).generate("plain", 0)
    assert inv.goods_receipt is False
    assert inv.received_date is None


def test_sampler_variant_is_still_deterministic() -> None:
    a = InvoiceSampler(goods_receipt=True, max_line_items=10).generate("gr", 5)
    b = InvoiceSampler(goods_receipt=True, max_line_items=10).generate("gr", 5)
    assert a == b


# ── A second, different hand ────────────────────────────────────────────────
def test_the_receiver_is_a_different_person_from_the_issuer() -> None:
    """Two identical hands on one page is what gives a synthetic document away."""
    for seed in range(60):
        hw = handwriting_for(seed, goods_receipt=True)
        assert hw.receiver_stack and hw.receiver_signature
        assert hw.receiver_stack != hw.writer_stack
        assert hw.receiver_signature != hw.signature_text


def test_the_receivers_printed_name_is_the_same_person_as_the_signature() -> None:
    """Regression: the two were sampled independently, so 'M. Sowande' signed and
    'S. SOWANDE' was printed underneath — two different people."""
    for seed in range(40):
        hw = handwriting_for(seed, goods_receipt=True)
        assert hw.receiver_name == hw.receiver_signature.upper()


def test_no_receiver_on_an_ordinary_handwritten_invoice() -> None:
    hw = handwriting_for(3)
    assert hw.receiver_signature == ""
    assert hw.receiver_stack == ""


def test_the_receiver_hand_is_deterministic() -> None:
    assert handwriting_for(9, goods_receipt=True) == handwriting_for(9, goods_receipt=True)


def test_the_pad_is_shortened_to_leave_room_for_the_receipt_block() -> None:
    """A delivery note that spills onto a second page puts the signature lines
    where nobody would sign them."""
    plain = handwriting_for(4, line_count=3)
    receipt = handwriting_for(4, line_count=3, goods_receipt=True)
    assert receipt.ruled_rows < plain.ruled_rows


# ── Rendering ───────────────────────────────────────────────────────────────
def test_the_receipt_block_renders_all_three_slots() -> None:
    html = render_record(PACK, goods_invoice())
    assert 'class="receipt-block"' in html
    for label in ("Received By", "Print Name", "Date Received"):
        assert label in html
    assert "Goods received in good condition." in html


def test_the_receipt_block_is_absent_without_the_flag() -> None:
    html = render_record(PACK, invoice(simple_lines()).model_copy(
        update={"condition": DocumentCondition.HANDWRITTEN}))
    assert 'class="receipt-block"' not in html
    assert "Received By" not in html


def test_the_receipt_block_prints_the_recorded_date() -> None:
    inv = goods_invoice(received_date=None)
    inv = type(inv).model_validate(
        {**inv.model_dump(), "received_date": inv.issue_date + timedelta(days=3)}
    )
    html = render_record(PACK, inv)
    assert inv.received_date.strftime("%m/%d/%Y") in html


# ── Golden data ─────────────────────────────────────────────────────────────
def test_the_flag_and_date_reach_the_golden_row() -> None:
    inv = goods_invoice()
    row = inv.to_rows()["invoices"][0]
    assert row["goods_receipt"] is True
    assert row["received_date"] == inv.received_date
