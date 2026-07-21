"""Document source — where records come from.

The seam between the document-agnostic worker loop and a pack's generation
logic. The worker knows how to claim units, render, persist, and account; it
does not know how to *invent* a document. That is the pack's job: an invoice
source samples a company, products, billing, tax, and locale, and returns a
computed :class:`GoldenRecord`.

The contract is one method and one invariant: ``generate(run_id, index)`` must
be a **pure function of its arguments**. Seeding from ``hash(run_id, index)``
makes a run reproducible and every retry idempotent — a re-run of a failed unit
produces byte-identical documents, so overwrite-on-retry is safe.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from docloom.core.record import GoldenRecord


def stable_seed(run_id: str, index: int) -> int:
    """A process-stable seed for document ``index`` of ``run_id``.

    Uses SHA-256, **not** the built-in ``hash()`` — Python salts string/tuple
    hashing per process (``PYTHONHASHSEED``), so ``hash((run_id, index))`` would
    give different values in different workers and silently break reproducibility
    and resume. Every source must seed from this.
    """
    digest = hashlib.sha256(f"{run_id}\x00{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


@runtime_checkable
class DocumentSource(Protocol):
    """Produces the ground-truth record for one document."""

    def generate(self, run_id: str, index: int) -> GoldenRecord:
        """Return the record for document ``index`` of ``run_id``.

        Deterministic: the same ``(run_id, index)`` must always yield the same
        record. Implementations seed their RNG from those two values and nothing
        else — no wall clock, no shared mutable state.
        """
        ...
