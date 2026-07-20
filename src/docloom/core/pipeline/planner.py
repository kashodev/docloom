"""Run planning — turning a document count into work units.

A run of ``total`` documents divides into contiguous units of ``unit_size``
indices; the last unit absorbs the remainder. The unit is the same boundary for
three things at once — the concurrency claim, the golden shard, and the export
read granularity — so there is exactly one number to reason about.

Units carry only index ranges, never document content. A worker regenerates its
unit's documents deterministically from ``hash(run_id, index)``, so a unit is
reproducible from its range alone and nothing needs to be handed between the
planner and the workers.
"""

from __future__ import annotations

from docloom.core.state.base import WorkUnit


def plan_units(run_id: str, total: int, unit_size: int) -> list[WorkUnit]:
    """Divide ``total`` documents into work units of ``unit_size``."""
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    if unit_size <= 0:
        raise ValueError(f"unit_size must be positive, got {unit_size}")

    units: list[WorkUnit] = []
    start = 0
    index = 0
    while start < total:
        count = min(unit_size, total - start)
        units.append(
            WorkUnit(run_id=run_id, unit_index=index, start_index=start, count=count)
        )
        start += count
        index += 1
    return units
