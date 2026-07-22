"""Spend-rollup and distributed-budget tests.

The rollup exists for two jobs: a live per-(run, model) total during a run, and a
budget that holds across a *fleet* rather than per process. The load-bearing
properties are that the counter is **exact**, that increments are **atomic**, and
that a cap enforced from one worker is visible to another.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from docloom.core.providers import CompletionRequest, ProviderMix
from docloom.core.providers.base import CompletionResult, Usage
from docloom.core.providers.budget import (
    BudgetExceeded,
    BudgetGuard,
    DistributedBudgetGuard,
)
from docloom.core.providers.pricing import pricing_for
from docloom.core.state import SqliteStateStore
from docloom.core.state.base import TOTAL_MODEL, from_nano, to_nano


def store(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "s.db")


# ── Nano-dollar conversion ──────────────────────────────────────────────────
def test_nano_keeps_a_sub_cent_cost() -> None:
    """The whole reason for nano rather than micro: a fraction of a cent must
    still register, or a million cheap calls sum to zero."""
    assert to_nano(D("0.0000004")) == 400
    assert from_nano(400) == D("0.0000004")


def test_one_token_at_the_cheapest_rate_still_counts() -> None:
    assert to_nano(D("0.05") / 1_000_000) == 50


def test_nano_round_trips_without_drift_over_many_calls() -> None:
    """Repeated quantisation must not accumulate error worth noticing."""
    per_call = D("0.0028")
    total = from_nano(sum(to_nano(per_call) for _ in range(100_000)))
    assert total == per_call * 100_000


def test_rounding_is_half_up_not_truncation() -> None:
    assert to_nano(D("0.0000000005")) == 1     # would be 0 if truncated
    assert to_nano(D("0.0000000004")) == 0


# ── The rollup ──────────────────────────────────────────────────────────────
def test_spend_accumulates_per_model_and_in_total(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_spend("r", "haiku", cost=D("0.001"))
    s.add_spend("r", "haiku", cost=D("0.002"))
    total = s.add_spend("r", "sonnet", cost=D("0.005"))

    assert total == D("0.008")
    by_model = {row.model: row for row in s.spend("r")}
    assert by_model["haiku"].cost_usd == D("0.003")
    assert by_model["haiku"].calls == 2
    assert by_model["sonnet"].cost_usd == D("0.005")
    assert by_model[TOTAL_MODEL].cost_usd == D("0.008")
    assert by_model[TOTAL_MODEL].calls == 3


def test_add_spend_returns_the_post_increment_total(tmp_path: Path) -> None:
    """A budget check needs the value *after* its own contribution, or two
    workers can both read "under budget" and both proceed."""
    s = store(tmp_path)
    assert s.add_spend("r", "m", cost=D("0.01")) == D("0.01")
    assert s.add_spend("r", "m", cost=D("0.01")) == D("0.02")


def test_tokens_accumulate_alongside_cost(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_spend("r", "m", cost=D("0.001"), input_tokens=800, output_tokens=400)
    s.add_spend("r", "m", cost=D("0.001"), input_tokens=200, output_tokens=100)
    row = next(r for r in s.spend("r") if r.model == "m")
    assert (row.input_tokens, row.output_tokens) == (1000, 500)


def test_runs_do_not_bleed_into_each_other(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_spend("r1", "m", cost=D("0.01"))
    s.add_spend("r2", "m", cost=D("0.02"))
    assert s.total_spend("r1") == D("0.01")
    assert s.total_spend("r2") == D("0.02")


def test_total_spend_of_an_untouched_run_is_zero(tmp_path: Path) -> None:
    assert store(tmp_path).total_spend("never-seen") == D(0)


def test_the_total_row_is_flagged_as_such(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_spend("r", "m", cost=D("0.01"))
    rows = {r.model: r for r in s.spend("r")}
    assert rows[TOTAL_MODEL].is_total is True
    assert rows["m"].is_total is False


def test_the_rollup_survives_reopening_the_store(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add_spend("r", "m", cost=D("0.01"))
    s.close()
    assert SqliteStateStore(tmp_path / "s.db").total_spend("r") == D("0.01")


def test_concurrent_increments_do_not_lose_updates(tmp_path: Path) -> None:
    """The reason this is an atomic upsert and not read-modify-write."""
    import threading

    db = tmp_path / "s.db"
    SqliteStateStore(db).add_spend("r", "m", cost=D(0), calls=0)

    def worker() -> None:
        s = SqliteStateStore(db)
        for _ in range(25):
            s.add_spend("r", "m", cost=D("0.001"))
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert SqliteStateStore(db).total_spend("r") == D("0.100")   # 4 x 25 x 0.001


# ── The distributed guard ───────────────────────────────────────────────────
def test_a_cap_holds_across_workers(tmp_path: Path) -> None:
    """The hole this closes: an in-process guard caps one worker, so N workers
    each honouring a $50 cap spend $50N."""
    s = store(tmp_path)
    a = DistributedBudgetGuard(s, "r", D("0.05"), flush_every=1)
    b = DistributedBudgetGuard(s, "r", D("0.05"), flush_every=1)

    for _ in range(3):
        a.add(D("0.01"), model="haiku")
    for _ in range(2):
        b.add(D("0.01"), model="sonnet")

    # b spent only $0.02 itself but must see the fleet's $0.05.
    assert b.spent == D("0.05")
    with pytest.raises(BudgetExceeded, match="run budget"):
        b.check(D("0.02"))


def test_an_in_process_guard_would_not_have_caught_it(tmp_path: Path) -> None:
    """Contrast, so the distributed guard's purpose is unambiguous."""
    a, b = BudgetGuard(D("0.05")), BudgetGuard(D("0.05"))
    for _ in range(3):
        a.add(D("0.01"))
    for _ in range(2):
        b.add(D("0.01"))
    b.check(D("0.02"))          # happily proceeds: it only knows its own $0.02
    assert a.spent + b.spent == D("0.05")


def test_spend_is_attributed_per_model(tmp_path: Path) -> None:
    s = store(tmp_path)
    g = DistributedBudgetGuard(s, "r", D("1.00"), flush_every=1)
    g.add(D("0.01"), model="haiku")
    g.add(D("0.02"), model="sonnet")
    by_model = {r.model: r.cost_usd for r in s.spend("r")}
    assert by_model["haiku"] == D("0.01")
    assert by_model["sonnet"] == D("0.02")


def test_batching_reduces_writes_and_still_totals_correctly(tmp_path: Path) -> None:
    s = store(tmp_path)
    g = DistributedBudgetGuard(s, "r", D("10.00"), flush_every=10)
    for _ in range(30):
        g.add(D("0.001"), model="haiku")
    g.flush()
    assert s.total_spend("r") == D("0.030")
    # 30 calls became 3 shared writes, not 30.
    assert next(r for r in s.spend("r") if r.model == "haiku").calls == 30


def test_unflushed_spend_still_counts_toward_the_local_view(tmp_path: Path) -> None:
    """Buffered spend is committed money; a check must not ignore it."""
    s = store(tmp_path)
    g = DistributedBudgetGuard(s, "r", D("1.00"), flush_every=100)
    g.add(D("0.25"), model="m")
    assert g.spent == D("0.25")
    assert s.total_spend("r") == D(0)          # not pushed yet
    assert g.remaining == D("0.75")


def test_check_refreshes_before_refusing(tmp_path: Path) -> None:
    """A worker's local view can be stale; it must consult the shared counter
    before rejecting work, or it would refuse on out-of-date information."""
    s = store(tmp_path)
    g = DistributedBudgetGuard(s, "r", D("1.00"), flush_every=100)
    s.add_spend("r", "other-worker", cost=D("0.99"))   # someone else spent it
    with pytest.raises(BudgetExceeded):
        g.check(D("0.02"))


def test_abort_off_lets_a_run_continue_past_the_limit(tmp_path: Path) -> None:
    s = store(tmp_path)
    g = DistributedBudgetGuard(s, "r", D("0.01"), abort_on_exceed=False, flush_every=1)
    for _ in range(5):
        g.add(D("0.01"), model="m")
    g.check(D("1.00"))                          # no raise
    assert g.spent == D("0.05")


def test_the_guard_is_interface_compatible_with_the_in_process_one(tmp_path: Path) -> None:
    """So it drops into ProviderMix unchanged."""
    s = store(tmp_path)
    for guard in (BudgetGuard(D("1.00")),
                  DistributedBudgetGuard(s, "r", D("1.00"), flush_every=1)):
        assert hasattr(guard, "check") and hasattr(guard, "add")
        assert guard.limit == D("1.00")
        guard.check(D("0.001"))
        guard.add(D("0.001"), model="m")
        assert guard.remaining < D("1.00")


# ── Through the provider mix ────────────────────────────────────────────────
class StubProvider:
    pricing = pricing_for("__local__")
    name, model = "anthropic", "claude-haiku-4-5"

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult("t", Usage(10, 5, 0), self.model, self.name, D("0.002"))

    def estimate_cost(self, request: CompletionRequest) -> D:
        return D("0.002")


def test_the_mix_records_spend_against_the_run(tmp_path: Path) -> None:
    s = store(tmp_path)
    mix = ProviderMix([StubProvider()], [1.0],
                      budget=DistributedBudgetGuard(s, "r", D("1.00"), flush_every=1))
    for _ in range(3):
        asyncio.run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))

    assert s.total_spend("r") == D("0.006")
    # ...and attributed to the model that actually did the work.
    assert next(r for r in s.spend("r") if r.model == "claude-haiku-4-5").calls == 3


def test_the_mix_stops_when_the_run_budget_is_gone(tmp_path: Path) -> None:
    s = store(tmp_path)
    mix = ProviderMix([StubProvider()], [1.0],
                      budget=DistributedBudgetGuard(s, "r", D("0.005"), flush_every=1))
    with pytest.raises(BudgetExceeded):
        for _ in range(10):
            asyncio.run(mix.complete(CompletionRequest(system="s", prompt="p"), seed=1))
    assert s.total_spend("r") <= D("0.006")
