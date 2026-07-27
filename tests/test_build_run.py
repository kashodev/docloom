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

from docsynth.core.enums import RunState
from docsynth.core.state.sqlite import SqliteStateStore
from docsynth.packs.invoice.artifact import load_catalogue, write_catalogue
from docsynth.packs.invoice.build_run import build_catalogue_run
from docsynth.packs.invoice.procedural import generate_catalogue
from docsynth.packs.invoice.sampler import InvoiceSampler


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
def test_a_failed_unit_leaves_no_root_manifest(tmp_path: Path, monkeypatch) -> None:
    """No root ⇒ do not consume it — the same completion contract as a run."""
    from docsynth.packs.invoice import build_run

    real = build_run.generate_company_range

    def explode_on_one_range(start, end, **kw):
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


def test_an_incomplete_build_never_reports_itself_complete(tmp_path: Path, monkeypatch) -> None:
    """The false-green that made a 1-of-10 build read as four successful tasks.

    A worker that claims nothing is not a success: the first attempt fails its
    units and exits non-zero, then the platform retries the task, the retry finds
    no PENDING units and drains cleanly. Unless completion is reported from the
    *build*, that retry exits 0 and the execution goes green over a broken build.
    """
    from docsynth.packs.invoice import build_run

    real = build_run.generate_company_range

    def explode(start, end, **kw):
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
    assert first.build_has_failures is True           # real holes → exit non-zero

    # The retry: re-queues the failed units, fails them again, and must still
    # report the build as incomplete rather than "nothing to do, all good".
    second = build_catalogue_run(state, **kw)         # type: ignore[arg-type]
    assert second.build_complete is False
    assert second.units_failed == 3, "failed units must be retried, not skipped"
    assert second.build_has_failures is True
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
    assert stats.build_has_failures is False


def test_a_build_reclaims_a_unit_a_crashed_worker_abandoned(tmp_path: Path, monkeypatch) -> None:
    """A unit left RUNNING with a lapsed lease by a killed/timed-out worker must be
    reclaimed at start. The Firestore claim does not scan for expired leases, so
    without an explicit reclaim such a unit is invisible to a re-run forever — the
    build claims nothing and can never finish. (Execution px8t4 recovered nothing
    for exactly this reason.)"""
    from datetime import UTC, datetime, timedelta

    from docsynth.core.pipeline.run import create_run
    from docsynth.packs.invoice.build_run import CATALOGUE_PACK

    out = str(tmp_path / "cat")
    state = _state(tmp_path)
    kw = dict(out=out, build_id="b", catalogue_version="v1", companies=40,
              products_per_company=6, unit_size=20, seed=1)                # 2 units

    create_run(state, run_id="b", pack=CATALOGUE_PACK, config_id="v1",
               total=40, unit_size=20)
    abandoned = state.claim_next_unit("b")                # a worker claims, then "crashes"
    assert abandoned is not None
    # Force its lease into the past — the worker died over a lease-length ago.
    state._conn.execute(                                  # type: ignore[attr-defined]
        "UPDATE work_units SET lease_expires_at=? WHERE run_id=? AND unit_index=?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), "b", abandoned.unit_index))
    state._conn.commit()                                 # type: ignore[attr-defined]

    reclaimed_for = []
    real = state.reclaim_expired_units
    monkeypatch.setattr(state, "reclaim_expired_units",
                        lambda rid, **k: (reclaimed_for.append(rid), real(rid, **k))[1])

    stats = build_catalogue_run(state, **kw)             # type: ignore[arg-type]
    assert "b" in reclaimed_for                          # the build ran a reclaim…
    assert stats.build_complete is True                  # …and finished both units
    assert len(load_catalogue(out).roster()) == 40


def test_a_worker_finishing_before_its_peers_is_not_a_failure(tmp_path: Path) -> None:
    """A worker that drained its share while a peer is still mid-unit must not
    report failure: the build simply is not globally done yet, and only real
    holes (failed units) are a failure. Reporting one as failed is what made
    execution qwcc2 pointlessly retry its healthy early-finishers.

    Simulated by leaving one unit claimed-but-unfinished (a stand-in for a peer
    that is still working it) while this worker drains the rest."""
    from docsynth.core.pipeline.run import create_run
    from docsynth.packs.invoice.build_run import CATALOGUE_PACK

    out = str(tmp_path / "cat")
    state = _state(tmp_path)
    kw = dict(out=out, build_id="b", catalogue_version="v1", companies=100,
              products_per_company=6, unit_size=20, seed=1)               # 5 units

    create_run(state, run_id="b", pack=CATALOGUE_PACK, config_id="v1",
               total=100, unit_size=20)
    peer_unit = state.claim_next_unit("b")            # a peer holds one, RUNNING
    assert peer_unit is not None

    stats = build_catalogue_run(state, **kw)          # type: ignore[arg-type]
    assert stats.units_completed == 4                 # drained the other four
    assert stats.build_complete is False              # the peer's unit is unfinished
    assert stats.build_has_failures is False          # …but no holes → NOT a failure


def test_a_resume_completes_the_build(tmp_path: Path, monkeypatch) -> None:
    from docsynth.core.pipeline import resume_run
    from docsynth.packs.invoice import build_run

    real = build_run.generate_company_range
    fail = {"on": True}

    def flaky(start, end, **kw):
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

    from docsynth.core.providers.base import CompletionResult, Usage
    from docsynth.core.providers.mix import ProviderMix
    from docsynth.core.providers.pricing import pricing_for

    class Empty:
        name = "f"
        model = "f-1"
        pricing = pricing_for("__local__")
        async def complete(self, request):
            return CompletionResult("", Usage(10, 5), self.model, self.name, D("0.001"))
        def estimate_cost(self, request):
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


def test_quarantine_persists_across_units_and_lands_in_the_manifest(tmp_path: Path) -> None:
    """R1 + R3 end to end: a provider the first unit finds dead is quarantined in
    the run state, so later units inherit it and make NO calls to re-learn it
    (R1), and the root manifest records which shards degraded and from whom (R3).
    """
    from decimal import Decimal as D

    from docsynth.core.providers.base import CompletionResult, Usage
    from docsynth.core.providers.mix import ProviderMix
    from docsynth.core.providers.pricing import pricing_for

    class Empty:
        name = "deepseek"
        model = "deepseek-v4-flash"
        pricing = pricing_for("__local__")
        calls = 0
        async def complete(self, request):
            type(self).calls += 1
            return CompletionResult("", Usage(10, 2000), self.model, self.name, D("0.001"))
        def estimate_cost(self, request):
            return D("0.001")

    out = str(tmp_path / "cat")
    state = _state(tmp_path)
    stats = build_catalogue_run(
        state, out=out, build_id="b", catalogue_version="v1",
        companies=60, products_per_company=6, unit_size=20, seed=2,  # 3 units of 20
        mix=ProviderMix([Empty()], [1.0]), max_rounds=1,
    )
    assert stats.build_complete and stats.units_failed == 0
    assert stats.procedural_fallback == 360               # all fell back, still complete

    # R1: the dead provider is quarantined at the build level…
    assert state.quarantined_providers("b") == {"deepseek"}
    # …and only the FIRST unit paid to discover it — the other two inherited the
    # quarantine and made zero calls (one unit's 20 items, not three units' 60).
    assert Empty.calls == 20

    # R3: the manifest records the degraded shards and who was quarantined.
    fb = load_catalogue(out).manifest.provenance.get("fallback")
    assert fb is not None
    assert fb["quarantined_providers"] == ["deepseek"]
    assert fb["shards_degraded"] == [0, 1, 2]


def test_a_budget_too_small_completes_procedurally_it_does_not_fail(tmp_path: Path) -> None:
    """A budget below the build's cost is a spend ceiling, not a failure — the
    cap stops further LLM calls and the rest of the catalogue falls back to
    procedural. This is the production bug behind execution ktwww: hitting the
    cap raised out of the recording path and failed whole units, so no budget
    under the full build cost could ever complete.

    A priced fake makes real calls cross a $0.05 cap; the build must still finish
    with a root manifest, every company populated, and some slots LLM-filled
    before the ceiling and the remainder procedural."""
    from decimal import Decimal as D

    from docsynth.core.providers.base import CompletionResult, Usage
    from docsynth.core.providers.mix import ProviderMix
    from docsynth.core.providers.pricing import pricing_for

    class Priced:
        name = "p"
        model = "p-1"
        pricing = pricing_for("__local__")
        async def complete(self, request):
            # A usable one-line description so filled slots are real, each $0.01.
            return CompletionResult('[{"description": "Widget, blue, each"}]',
                                    Usage(50, 20), self.model, self.name, D("0.01"))
        def estimate_cost(self, request):
            return D("0.01")

    out = str(tmp_path / "cat")
    stats = build_catalogue_run(
        _state(tmp_path), out=out, build_id="b", catalogue_version="v1",
        companies=40, products_per_company=10, unit_size=20, seed=2,
        mix=ProviderMix([Priced()], [1.0]), budget_usd=0.05, max_rounds=2,
    )
    assert stats.units_failed == 0                    # the cap must not fail a unit
    assert stats.build_complete is True               # …the build still completes
    assert stats.llm_filled > 0                        # some slots filled before the cap
    assert stats.procedural_fallback > 0               # the rest fell back after it
    # A soft cap: once crossed, no new work goes out, but a chunk already in
    # flight still records — so spend lands near the cap, far below the $4.00
    # (400 × $0.01) an uncapped full build would cost. The point is bounded, not
    # exact.
    assert stats.total_cost < D("1.00")
    cat = load_catalogue(out)
    assert len(cat.roster()) == 40
    assert all(cat.spec_for(c).products for c in cat.roster().companies)
