"""Hand-filled invoice tests.

Two things matter here. First, the handwriting is *deterministic* — the same
document always draws the same writer, jitter, signature and stamp. Second, and
load-bearing: rendering an invoice by hand must not change a single value, so a
handwritten document and its clean twin produce **identical golden rows**. A
realistic artefact whose labels drifted would be worse than no artefact at all.
"""

from __future__ import annotations

import docloom.packs  # noqa: F401 - registers the invoice pack
from docloom.core import get_pack
from docloom.core.enums import DocumentCondition
from docloom.core.render import render_record
from docloom.packs.invoice import HANDWRITTEN_ARCHETYPE
from docloom.packs.invoice.fonts import HANDWRITING_KEYS
from docloom.packs.invoice.handwriting import handwriting_for
from tests.factories import invoice, simple_lines

PACK = get_pack("invoice")


def handwritten(**kw):  # noqa: ANN003, ANN201
    inv = invoice(simple_lines(), **kw)
    return inv.model_copy(update={"condition": DocumentCondition.HANDWRITTEN})


# ── Determinism ─────────────────────────────────────────────────────────────
def test_same_seed_gives_the_same_hand() -> None:
    a, b = handwriting_for(4242), handwriting_for(4242)
    assert a == b


def test_different_seeds_differ() -> None:
    seeds = {handwriting_for(s).writer_key + handwriting_for(s).signature_text
             for s in range(40)}
    assert len(seeds) > 1


def test_jitter_is_deterministic_and_bounded() -> None:
    hw = handwriting_for(7)
    assert hw.jitter(3) == hw.jitter(3)
    for i in range(200):
        j = hw.jitter(i)
        assert -2.0 <= j.rotate <= 2.0        # a slight lean, not a spin
        assert -3.0 <= j.dy <= 2.0            # sits near the rule
        assert 0.9 <= j.scale <= 1.12
    assert "rotate(" in hw.jitter(0).css and "translateY(" in hw.jitter(0).css


def test_jitter_wraps_so_any_field_count_is_covered() -> None:
    hw = handwriting_for(11)
    assert hw.jitter(0) == hw.jitter(37)      # pool size, wrapped


# ── The legibility dial ─────────────────────────────────────────────────────
def test_legibility_selects_the_neatest_and_messiest_hands() -> None:
    assert handwriting_for(1, legibility=1.0).writer_key == HANDWRITING_KEYS[0]
    assert handwriting_for(1, legibility=0.0).writer_key == HANDWRITING_KEYS[-1]


def test_legibility_is_monotonic_across_the_range() -> None:
    """Lower legibility never picks a neater hand than higher legibility."""
    order = {k: i for i, k in enumerate(HANDWRITING_KEYS)}
    picks = [order[handwriting_for(3, legibility=x / 10).writer_key] for x in range(11)]
    assert picks == sorted(picks, reverse=True)


# ── The document's marks ────────────────────────────────────────────────────
def test_three_faces_are_embedded_writer_signature_stamp() -> None:
    hw = handwriting_for(99)
    assert hw.face_css.count("@font-face") == 3


def test_signature_is_synthetic_and_non_empty() -> None:
    for seed in range(30):
        text = handwriting_for(seed).signature_text
        assert text and "." in text          # "M. Sowande" shape
        assert len(text) < 24


def test_stamp_is_a_stock_office_mark_in_pad_ink() -> None:
    hw = handwriting_for(5)
    assert hw.stamp_text in {"PAID", "RECEIVED", "APPROVED", "ENTERED"}
    assert hw.stamp_ink.startswith("#")
    assert -20 < hw.stamp_rotate < 0          # stamps land askew, never square


def test_pad_always_has_blank_rules_after_the_last_item() -> None:
    for lines in (1, 5, 12, 30):
        hw = handwriting_for(2, line_count=lines)
        assert hw.ruled_rows >= lines + 2
        assert hw.ruled_rows >= 14            # reaches the foot of the pad


# ── Archetype routing ───────────────────────────────────────────────────────
def test_handwritten_condition_routes_to_the_pad_archetype() -> None:
    assert PACK.archetype_for(handwritten()) == HANDWRITTEN_ARCHETYPE


def test_other_conditions_keep_the_company_archetype() -> None:
    for condition in (DocumentCondition.CLEAN, DocumentCondition.LIGHT_SCAN,
                      DocumentCondition.HEAVY_SCAN):
        inv = invoice(simple_lines()).model_copy(update={"condition": condition})
        assert PACK.archetype_for(inv) == inv.render_profile.archetype


# ── The golden invariant ────────────────────────────────────────────────────
def test_handwriting_does_not_change_a_single_golden_value() -> None:
    """The whole point: only the rendering differs, never the ground truth."""
    clean = invoice(simple_lines())
    hand = clean.model_copy(update={"condition": DocumentCondition.HANDWRITTEN})

    clean_rows, hand_rows = clean.to_rows(), hand.to_rows()
    assert set(clean_rows) == set(hand_rows)
    for table, rows in clean_rows.items():
        hand_table = hand_rows[table]
        # Every column except the recorded condition flags must be identical.
        for a, b in zip(rows, hand_table, strict=True):
            differing = {k for k in a if a[k] != b.get(k)}
            assert differing <= {"condition", "is_handwritten", "is_degraded"}, differing


def test_handwritten_render_prints_the_records_own_figures() -> None:
    inv = handwritten()
    html = render_record(PACK, inv)
    # The grand total is written by hand, but it is the computed one.
    assert "210.00" in html
    assert inv.invoice_number in html
    assert inv.recipient.name in html


def test_handwritten_render_embeds_the_hand_and_marks() -> None:
    html = render_record(PACK, handwritten())
    assert html.count("@font-face") >= 3        # writer + signature + stamp
    assert 'class="hand"' in html               # values are in the writer's hand
    assert 'class="signature"' in html
    assert 'class="stamp"' in html
    assert "feTurbulence" in html                # ink roughening, not clean vectors


def test_clean_render_pays_for_none_of_it() -> None:
    """A normal invoice must not carry three extra embedded faces."""
    html = render_record(PACK, invoice(simple_lines()))
    assert 'class="hand"' not in html
    assert "feTurbulence" not in html
