"""Budget guard.

A catalogue run has a hard dollar ceiling (the $50 in DESIGN.md). The guard
accumulates spend and, when configured to abort, refuses to start work it cannot
afford — a pre-flight estimate check before each call, plus the actual cost
after. It is the difference between a run that stops at $50 and one that
discovers it spent $200 after the fact.

Thread/async-safe via a lock: catalogue generation runs many completions
concurrently, so ``spend`` is mutated from multiple tasks.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal


class BudgetExceeded(RuntimeError):
    """Raised when a completion would push cumulative spend over the limit."""


class BudgetGuard:
    """Tracks spend against a ceiling."""

    def __init__(self, limit_usd: Decimal, *, abort_on_exceed: bool = True) -> None:
        self._limit = limit_usd
        self._abort = abort_on_exceed
        self._spent = Decimal(0)
        self._lock = threading.Lock()

    @property
    def limit(self) -> Decimal:
        return self._limit

    @property
    def spent(self) -> Decimal:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> Decimal:
        with self._lock:
            return self._limit - self._spent

    def check(self, estimate: Decimal) -> None:
        """Pre-flight: raise if this estimated cost would breach the limit.

        Checked before spending so a run stops *before* the offending call, not
        after. A zero/near-zero limit disables the check only when abort is off.
        """
        if not self._abort:
            return
        with self._lock:
            if self._spent + estimate > self._limit:
                raise BudgetExceeded(
                    f"estimated ${estimate:.4f} would exceed the ${self._limit:.2f} "
                    f"budget (spent ${self._spent:.4f})"
                )

    def add(self, cost: Decimal, *, model: str = "") -> None:
        """Record actual spend after a completion.

        ``model`` is accepted and ignored so this stays interface-compatible with
        :class:`DistributedBudgetGuard`, which uses it to attribute spend per
        model — callers can pass it unconditionally.
        """
        with self._lock:
            self._spent += cost
            if self._abort and self._spent > self._limit:
                raise BudgetExceeded(
                    f"budget exceeded: spent ${self._spent:.4f} of ${self._limit:.2f}"
                )


class DistributedBudgetGuard:
    """A budget shared across every worker on a run.

    :class:`BudgetGuard` caps **one process**. Two hundred workers each honouring
    a $50 ceiling will spend $10,000 — the guard is doing exactly what it was
    built to do, and the fleet still blows the budget. This one counts against the
    run's spend rollup in the StateStore, which every worker reaches by
    definition, so the cap is global.

    Interface-compatible with :class:`BudgetGuard` (``check`` / ``add`` /
    ``limit`` / ``spent`` / ``remaining``), so it drops into ``ProviderMix``
    unchanged.

    **The trade-off, stated plainly.** Perfect enforcement means a shared read and
    write on every call, which is a hot key and a round trip per LLM call. Two
    dials bound the two kinds of staleness instead:

    * ``flush_every`` — how many calls of *our own* spend to buffer before pushing.
      Bounds how far the shared total lags reality: at most
      ``flush_every × workers × cost-per-call``.
    * ``refresh_interval`` — how long we may go without re-reading the shared
      total. Bounds how stale our view of *other workers* is, in wall-clock time
      rather than call count, so it holds regardless of how fast the fleet runs.

    ``flush_every=1, refresh_interval=0`` is exact and chattiest. The defaults
    accept a bounded, predictable overshoot for roughly one write per batch and
    one read per interval. There is no setting that is both exact and cheap, so
    these are dials rather than a default someone has to discover.
    """

    def __init__(
        self,
        state: object,
        run_id: str,
        limit_usd: Decimal,
        *,
        abort_on_exceed: bool = True,
        flush_every: int = 20,
        refresh_interval: float = 5.0,
    ) -> None:
        self._state = state
        self._run_id = run_id
        self._limit = limit_usd
        self._abort = abort_on_exceed
        self._flush_every = max(int(flush_every), 1)
        self._refresh_interval = max(float(refresh_interval), 0.0)
        self._lock = threading.Lock()
        #: Monotonic time of the last shared read; None until the first one, so
        #: the very first check always consults the shared counter rather than
        #: trusting an empty local view.
        self._refreshed_at: float | None = None
        #: Spend accumulated locally but not yet pushed to the shared counter.
        self._pending = Decimal(0)
        self._pending_calls = 0
        self._pending_model: str | None = None
        #: Last known shared total, refreshed on every flush.
        self._shared = Decimal(0)

    @property
    def limit(self) -> Decimal:
        return self._limit

    @property
    def spent(self) -> Decimal:
        """Shared total plus anything not yet pushed — what has actually been
        committed to, which is the number a check must use."""
        with self._lock:
            return self._shared + self._pending

    @property
    def remaining(self) -> Decimal:
        return self._limit - self.spent

    def check(self, estimate: Decimal) -> None:
        """Pre-flight: refuse work the run cannot afford.

        Refreshes from the shared counter when the local view has gone stale —
        *before* deciding, not after. Checking a stale view first would let a
        worker that has spent nothing itself sail past a cap the rest of the
        fleet already blew, which is exactly the failure this class exists to
        prevent.
        """
        if not self._abort:
            return
        if self._is_stale():
            self.flush()
        if self.spent + estimate > self._limit:
            self.flush()          # last word from the shared counter
            if self.spent + estimate > self._limit:
                raise BudgetExceeded(
                    f"estimated ${estimate:.4f} would exceed the ${self._limit:.2f} "
                    f"run budget (fleet has spent ${self.spent:.4f})"
                )

    def _is_stale(self) -> bool:
        """True when we have never read the shared total, or not recently enough."""
        with self._lock:
            last = self._refreshed_at
        if last is None:
            return True
        return (time.monotonic() - last) >= self._refresh_interval

    def add(self, cost: Decimal, *, model: str = "unknown") -> None:
        """Record actual spend, pushing to the shared counter every so often."""
        with self._lock:
            self._pending += cost
            self._pending_calls += 1
            self._pending_model = model
            due = self._pending_calls >= self._flush_every
        if due:
            self.flush()
        if self._abort and self.spent > self._limit:
            raise BudgetExceeded(
                f"run budget exceeded: ${self.spent:.4f} of ${self._limit:.2f}"
            )

    def flush(self) -> Decimal:
        """Push buffered spend to the shared counter; returns the fleet total."""
        with self._lock:
            pending, model = self._pending, self._pending_model or "unknown"
            calls = self._pending_calls
            self._pending, self._pending_calls = Decimal(0), 0
        if pending or calls:
            self._shared = self._state.add_spend(   # type: ignore[attr-defined]
                self._run_id, model, cost=pending, calls=calls
            )
        else:
            self._shared = self._state.total_spend(self._run_id)  # type: ignore[attr-defined]
        with self._lock:
            self._refreshed_at = time.monotonic()
        return self._shared

    def close(self) -> None:
        self.flush()
