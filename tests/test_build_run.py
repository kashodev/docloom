"""Distributed catalogue build tests — a build is a run.

The properties that matter mirror the generation run: many workers split the
company units with no collision, the artifact is sharded and complete, a failed
unit leaves no root manifest (so a consumer never reads a partial build as done),
a resume finishes it, and the LLM path falls back the same way. Determinism of
the *content* holds because companies are per-index seeded, so a sharded build
and a single-file build produce the same catalogue.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from docloom.core.enums import RunState
from docloom.packs.invoice.artifact import load_catalogue, write_catalogue
from docloom.packs.invoice.build_run import build_catalogue_run
from docloom.packs.invoice.procedural import generate_catalogue
from docloom.packs.invoice.sampler import InvoiceSampler
from docloom.core.state.sqlite import SqliteStateStore


def _state(tmp_path: Path, name: str = "b.db") -> SqliteStateStore:
    return SqliteStateStore(tmp_path / name)


# ── The build produces a complete sharded artifact ──────────────────────────
def test_a_procedural_build_writes_a_sharded_artifact(tmp_path: Path) -> None:
    out = str(tmp_path / "cat")
    stats = build_catalogue_run(
        _state(tmp_path), out=out, build_id="b", catalogue_version="v1",
        companies=100, products_per_company=10, unit_size=25, seed=3,
    )
    assert stats.units_completed == 4 and stats.units_failed == 0

    cat = load_catalogue(out)
    assert cat.manifest.is_sharded and len(cat.manifest.shards) == 4
    assert len(cat.roster()) == 100
    ids = sorted(c.company_id for c in cat.roster().companies)
    assert ids == [f"c{i:06d}" for i in range(100)]     # no gaps, no duplicates
    assert cat.manifest.provenance["products"] == 1000


def test_sharded_matches_a_single_file_build(tmp_path: Path) -> None:
    """Per-index seeding means the shape of the build does not change the content."""
    out = str(tmp_path / "sharded")
    build_catalogue_run(_state(tmp_path), out=out, build_id="b", catalogue_version="v1",
                        companies=60, products_per_company=8, unit_size=20, seed=5)
    single_out = str(tmp_path / "single")
    rows, products = generate_catalogue(companies=60, products_per_company=8, seed=5)
    write_catalogue(single_out, companies=rows, products=products, catalogue_version="v1")

    sharded = {c.company_id: c.name for c in load_catalogue(out).roster().companies}
    single = {c.company_id: c.name for c in load_catalogue(single_out).roster().companies}
    assert sharded == single


# ── Concurrency ─────────────────────────────────────────────────────────────
def test_many_workers_split_the_units_without_collision(tmp_path: Path) -> None:
    """Two workers against one build — exactly two Cloud Run tasks. The atomic
    claim gives each disjoint units, and whoever finishes last writes the root."""
    out = str(tmp_path / "cat")
    db = tmp_path / "b.db"
    SqliteStateStore(db).close()
    done: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        s = SqliteStateStore(db)
        stats = build_catalogue_run(s, out=out, build_id="b", catalogue_version="v1",
                                    companies=100, products_per_company=6,
                                    unit_size=20, seed=1)
        with lock:
            done.append(stats.units_completed)
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(done) == 5                       # 100 / 20, split between the two
    cat = load_catalogue(out)
    assert len(cat.roster()) == 100             # complete despite two writers
    assert len(cat.manifest.shards) == 5


# ── A partial build is not mistaken for complete ────────────────────────────
def test_a_failed_unit_leaves_no_root_manifest(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """No root ⇒ do not consume it — the same completion contract as a run."""
    from docloom.packs.invoice import build_run

    real = build_run.generate_company_range

    def explode_on_one_range(start, end, **kw):  # noqa: ANN001, ANN003
        if start == 20:
            raise RuntimeError("boom")
        return real(start, end, **kw)

    monkeypatch.setattr(build_run, "generate_company_range", explode_on_one_range)
    out = str(tmp_path / "cat")
    stats = build_catalogue_run(_state(tmp_path), out=out, build_id="b",
                                catalogue_version="v1", companies=100,
                                products_per_company=6, unit_size=20, seed=1)
    assert stats.units_failed == 1
    with pytest.raises(FileNotFoundError):
        load_catalogue(out)                     # no root manifest for a partial build


def test_an_incomplete_build_never_reports_itself_complete(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The false-green that made a 1-of-10 build read as four successful tasks.

    A worker that claims nothing is not a success: the first attempt fails its
    units and exits non-zero, then the platform retries the task, the retry finds
    no PENDING units and drains cleanly. Unless completion is reported from the
    *build*, that retry exits 0 and the execution goes green over a broken build.
    """
    from docloom.packs.invoice import build_run

    real = build_run.generate_company_range

    def explode(start, end, **kw):  # noqa: ANN001, ANN003
        if start >= 40:
            raise RuntimeError("provider is out of credit")
        return real(start, end, **kw)

    monkeypatch.setattr(build_run, "generate_company_range", explode)
    out = str(tmp_path / "cat")
    state = _state(tmp_path)
    kw = dict(out=out, build_id="b", catalogue_version="v1", companies=100,
              products_per_company=6, unit_size=20, seed=1)

    first = build_catalogue_run(state, **kw)          # type: ignore[arg-type]
    assert first.units_failed == 3 and first.build_complete is False

    # The retry: re-queues the failed units, fails them again, and must still
    # report the build as incomplete rather than "nothing to do, all good".
    second = build_catalogue_run(state, **kw)         # type: ignore[arg-type]
    assert second.build_complete is False
    assert second.units_failed == 3, "failed units must be retried, not skipped"
    with pytest.raises(FileNotFoundError):
        load_catalogue(out)


def test_a_worker_that_claims_nothing_reports_the_builds_state(tmp_path: Path) -> None:
    """A second worker arriving after the build is done exits successfully; one
    arriving to a half-built run does not."""
    out = str(tmp_path / "cat")
    db = tmp_path / "b.db"
    kw = dict(out=out, build_id="b", catalogue_version="v1", companies=40,
              products_per_company=6, unit_size=20, seed=1)
    first = SqliteStateStore(db)
    build_catalogue_run(first, **kw)                  # type: ignore[arg-type]
    first.close()

    latecomer = SqliteStateStore(db)
    stats = build_catalogue_run(latecomer, **kw)      # type: ignore[arg-type]
    latecomer.close()
    assert stats.units_completed == 0                 # nothing left to claim
    assert stats.build_complete is True               # …but the build IS done


def test_a_resume_completes_the_build(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from docloom.core.pipeline import resume_run
    from docloom.packs.invoice import build_run

    real = build_run.generate_company_range
    fail = {"on": True}

    def flaky(start, end, **kw):  # noqa: ANN001, ANN003
        if start == 20 and fail["on"]:
            raise RuntimeError("boom")
        return real(start, end, **kw)

    monkeypatch.setattr(build_run, "generate_company_range", flaky)
    out = str(tmp_path / "cat")
    state = _state(tmp_path)
    build_catalogue_run(state, out=out, build_id="b", catalogue_version="v1",
                        companies=100, products_per_company=6, unit_size=20, seed=1)
    with pytest.raises(FileNotFoundError):
        load_catalogue(out)

    fail["on"] = False
    resume_run(state, "b")
    build_catalogue_run(state, out=out, build_id="b", catalogue_version="v1",
                        companies=100, products_per_company=6, unit_size=20, seed=1)

    cat = load_catalogue(out)
    assert len(cat.roster()) == 100
    assert state.get_run("b").state is RunState.COMPLETED   # type: ignore[union-attr]


# ── Generating from the built artifact ──────────────────────────────────────
def test_invoices_generate_from_a_sharded_catalogue(tmp_path: Path) -> None:
    out = str(tmp_path / "cat")
    build_catalogue_run(_state(tmp_path), out=out, build_id="b", catalogue_version="cat-2026",
                        companies=40, products_per_company=20, unit_size=10, seed=4)
    sampler = InvoiceSampler(load_catalogue(out), max_line_items=8)
    invoices = [sampler.generate("r", i) for i in range(20)]
    assert all(inv.totals.grand_total > 0 for inv in invoices)
    assert all(inv.catalogue_version == "cat-2026" for inv in invoices)


# ── The LLM path shards too ─────────────────────────────────────────────────
def test_the_llm_build_shards_and_falls_back(tmp_path: Path) -> None:
    """A fake provider stands in for the real one; the empty responder forces the
    procedural fallback, proving the sharded LLM path degrades the same way."""
    from decimal import Decimal as D

    from docloom.core.providers.base import CompletionResult, Usage
    from docloom.core.providers.mix import ProviderMix
    from docloom.core.providers.pricing import pricing_for

    class Empty:
        name = "f"; model = "f-1"; pricing = pricing_for("__local__")
        async def complete(self, request):  # noqa: ANN001
            return CompletionResult("", Usage(10, 5), self.model, self.name, D("0.001"))
        def estimate_cost(self, request):  # noqa: ANN001
            return D("0.001")

    out = str(tmp_path / "cat")
    stats = build_catalogue_run(
        _state(tmp_path), out=out, build_id="b", catalogue_version="v1",
        companies=40, products_per_company=10, unit_size=20, seed=2,
        mix=ProviderMix([Empty()], [1.0]), max_rounds=1,
    )
    assert stats.units_completed == 2
    assert stats.procedural_fallback == 400          # all fell back — still complete
    cat = load_catalogue(out)
    assert len(cat.roster()) == 40
    assert all(cat.spec_for(c).products for c in cat.roster().companies)
