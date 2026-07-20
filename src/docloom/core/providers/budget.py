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

    def add(self, cost: Decimal) -> None:
        """Record actual spend after a completion."""
        with self._lock:
            self._spent += cost
            if self._abort and self._spent > self._limit:
                raise BudgetExceeded(
                    f"budget exceeded: spent ${self._spent:.4f} of ${self._limit:.2f}"
                )
