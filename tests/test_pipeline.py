"""Generation pipeline tests — the spine, end to end.

Not mocked: a source producing real multi-locale ``GoldenInvoice``s, the real
``HtmlRenderer`` with the invoice pack, a real SQLite state store, and a real
local blob store. The only thing absent is the PDF renderer (HTML stands in) and
the catalogue-based sampler (a deterministic source stands in). What's asserted
is the run lifecycle, deterministic generation, golden-shard exactness, and
failure/resume — the properties a real run depends on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

import docloom.packs  # noqa: F401  — registers the invoice pack
from docloom.core import Currency, Jurisdiction, Locale, RunState, WorkUnitState, get_pack
from docloom.core.pipeline import (
    HtmlRenderer,
    create_run,
    decode_shard,
    encode_shard,
    plan_units,
    resume_run,
    work_run,
)
from docloom.core.state.sqlite import SqliteStateStore
from docloom.core.storage.local import LocalBlobStore
from tests.factories import invoice, simple_lines

_VARIANTS = [
    (Locale.EN_US, Jurisdiction.US, Currency.USD),
    (Locale.EN_GB, Jurisdiction.GB, Currency.GBP),
    (Locale.FR_FR, Jurisdiction.FR, Currency.EUR),
]


class InvoiceSource:
    """A deterministic stand-in for the catalogue-based sampler.

    Pure function of (run_id, index): rotates locale for variety, stamps a
    per-index id. Real enough to exercise multi-locale rendering through the
    whole pipeline.
    """

    def generate(self, run_id: str, index: int):  # noqa: ANN201
        locale, juris, currency = _VARIANTS[index % len(_VARIANTS)]
        inv = invoice(simple_lines(), locale=locale, jurisdiction=juris, currency=currency)
        return inv.model_copy(update={
            "invoice_id": f"inv_{index:06d}",
            "invoice_index": index,
            "seed": hash((run_id, index)) & 0xFFFFFFFF,
        })


class FlakySource(InvoiceSource):
    def __init__(self, fail_on: set[int]) -> None:
        self._fail_on = fail_on

    def generate(self, run_id: str, index: int):  # noqa: ANN201
        if index in self._fail_on:
            raise ValueError(f"boom at index {index}")
        return super().generate(run_id, index)


def harness(tmp_path: Path):  # noqa: ANN201
    state = SqliteStateStore(tmp_path / "runs.db")
    blob = LocalBlobStore(tmp_path / "blobs")
    renderer = HtmlRenderer(get_pack("invoice"))
    return state, blob, renderer


# ─────────────────────────────────────────────────────────────────────────────
# Planner
# ─────────────────────────────────────────────────────────────────────────────

def test_plan_units_divides_with_a_remainder() -> None:
    units = plan_units("r", total=7, unit_size=3)
    assert [(u.start_index, u.count) for u in units] == [(0, 3), (3, 3), (6, 1)]
    assert [u.unit_index for u in units] == [0, 1, 2]


def test_plan_units_exact_multiple() -> None:
    units = plan_units("r", total=250, unit_size=100)
    assert [u.count for u in units] == [100, 100, 50]


def test_plan_units_smaller_than_one_unit() -> None:
    units = plan_units("r", total=5, unit_size=1000)
    assert len(units) == 1 and units[0].count == 5


@pytest.mark.parametrize("bad", [(0, 10), (10, 0), (-1, 10)])
def test_plan_units_rejects_nonpositive(bad: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        plan_units("r", total=bad[0], unit_size=bad[1])


# ─────────────────────────────────────────────────────────────────────────────
# Golden codec — exactness across the JSON round trip
# ─────────────────────────────────────────────────────────────────────────────

def test_shard_preserves_decimal_and_date_exactly() -> None:
    rows = [
        {"grand_total": D("327.02"), "issue_date": date(2026, 7, 15), "n": 3, "ok": True},
        {"grand_total": D("0.0028"), "issue_date": None, "n": None, "ok": False},
    ]
    back = decode_shard(encode_shard(rows))
    assert back == rows
    assert isinstance(back[0]["grand_total"], D)          # not float, not str
    assert isinstance(back[0]["issue_date"], date)
    assert back[1]["grand_total"] == D("0.0028")


def test_empty_shard_roundtrips() -> None:
    assert decode_shard(encode_shard([])) == []


def test_shard_preserves_list_columns() -> None:
    rows = [{"billing_models": ["flat_rate", "usage"]}]
    assert decode_shard(encode_shard(rows)) == rows


# ─────────────────────────────────────────────────────────────────────────────
# Source determinism
# ─────────────────────────────────────────────────────────────────────────────

def test_source_is_deterministic() -> None:
    src = InvoiceSource()
    assert src.generate("run_x", 5) == src.generate("run_x", 5)


def test_source_varies_by_index() -> None:
    src = InvoiceSource()
    assert src.generate("run_x", 0).locale != src.generate("run_x", 1).locale


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end run
# ─────────────────────────────────────────────────────────────────────────────

def test_run_generates_documents_and_golden_shards(tmp_path: Path) -> None:
    state, blob, renderer = harness(tmp_path)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=7, unit_size=3)

    stats = work_run(state, run_id="r", source=InvoiceSource(), renderer=renderer, blob=blob)

    # Lifecycle
    assert stats.units_completed == 3
    assert stats.units_failed == 0
    assert stats.documents_written == 7
    assert state.get_run("r").state is RunState.COMPLETED
    assert state.progress("r")[WorkUnitState.DONE] == 3

    # One document per index.
    docs = list(blob.iter_keys("r/documents/"))
    assert len(docs) == 7
    assert docs[0].endswith(".html")
    assert b"<!doctype html>" in blob.get(docs[0])

    # One shard per (unit, table): 3 units x {invoices, line_items} = 6.
    shards = list(blob.iter_keys("r/golden/"))
    assert len(shards) == 6
    assert sum("/invoices/" in k for k in shards) == 3
    assert sum("/line_items/" in k for k in shards) == 3


def test_golden_shards_hold_exact_ground_truth(tmp_path: Path) -> None:
    state, blob, renderer = harness(tmp_path)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=3, unit_size=3)
    work_run(state, run_id="r", source=InvoiceSource(), renderer=renderer, blob=blob)

    inv_shard = next(k for k in blob.iter_keys("r/golden/invoices/"))
    rows = decode_shard(blob.get(inv_shard))
    assert len(rows) == 3
    assert isinstance(rows[0]["grand_total"], D)
    assert rows[0]["grand_total"] == D("210.00")           # from simple_lines()
    assert {r["invoice_id"] for r in rows} == {"inv_000000", "inv_000001", "inv_000002"}


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    """Deterministic generation + overwrite-on-retry: a second full run leaves
    the same document count, not duplicates."""
    state, blob, renderer = harness(tmp_path)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=4, unit_size=2)
    work_run(state, run_id="r", source=InvoiceSource(), renderer=renderer, blob=blob)
    first = list(blob.iter_keys("r/documents/"))

    resume_run(state, "r")                                  # nothing failed → no-op reset
    assert list(blob.iter_keys("r/documents/")) == first


def test_failure_marks_unit_and_worker_continues(tmp_path: Path) -> None:
    state, blob, renderer = harness(tmp_path)
    # 3 units of 2: [0,1] [2,3] [4,5]; fail the middle unit at index 2.
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=6, unit_size=2)

    stats = work_run(state, run_id="r", source=FlakySource({2}), renderer=renderer, blob=blob)

    assert stats.units_completed == 2      # units 0 and 2 still done — worker did not abort
    assert stats.units_failed == 1
    assert state.get_run("r").state is RunState.RUNNING     # not COMPLETED: a unit failed
    progress = state.progress("r")
    assert progress[WorkUnitState.DONE] == 2
    assert progress[WorkUnitState.FAILED] == 1


def test_resume_after_failure_completes_the_run(tmp_path: Path) -> None:
    state, blob, renderer = harness(tmp_path)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=6, unit_size=2)
    work_run(state, run_id="r", source=FlakySource({2}), renderer=renderer, blob=blob)

    # Resume with a healthy source — the failed unit is retried, nothing else is.
    requeued = resume_run(state, "r")
    assert requeued == 1
    stats = work_run(state, run_id="r", source=InvoiceSource(), renderer=renderer, blob=blob)

    assert stats.units_completed == 1                       # only the previously-failed unit
    assert state.get_run("r").state is RunState.COMPLETED
    assert len(list(blob.iter_keys("r/documents/"))) == 6


def test_two_workers_split_the_units_without_overlap(tmp_path: Path) -> None:
    """The multi-worker case in miniature: interleave two workers' claims and
    assert every unit is done exactly once."""
    state, blob, renderer = harness(tmp_path)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=10, unit_size=2)

    from docloom.core.pipeline.worker import GenerationWorker

    src, r = InvoiceSource(), renderer
    w1 = GenerationWorker(run_id="r", source=src, renderer=r, blob=blob, state=state)
    w2 = GenerationWorker(run_id="r", source=src, renderer=r, blob=blob, state=state)
    s1, s2 = w1.run(), w2.run()

    assert s1.units_completed + s2.units_completed == 5
    assert state.progress("r")[WorkUnitState.DONE] == 5
    assert len(list(blob.iter_keys("r/documents/"))) == 10   # each index exactly once


def test_a_source_that_cannot_prepare_stops_the_run_before_any_unit(tmp_path) -> None:  # noqa: ANN001
    """An impossible run-scoped configuration must fail once, up front — not
    once per unit while the job reports success."""
    from docloom.core.pipeline.source import prepare_source

    class Refuses:
        def __init__(self) -> None:
            self.generated = 0

        def prepare(self, run_id: str) -> None:
            raise ValueError("no company issues in de-DE")

        def generate(self, run_id: str, index: int):  # noqa: ANN202
            self.generated += 1
            raise AssertionError("must not be reached")

    source = Refuses()
    with pytest.raises(ValueError, match="de-DE"):
        prepare_source(source, "r")
    assert source.generated == 0


def test_a_source_without_prepare_is_fine() -> None:
    """Optional by design: most sources need nothing resolved up front."""
    from docloom.core.pipeline.source import prepare_source

    class Plain:
        def generate(self, run_id: str, index: int):  # noqa: ANN202
            raise AssertionError

    prepare_source(Plain(), "r")   # must not raise
