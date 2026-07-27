"""Sharded usage sink — the default, and the one that needs no new infrastructure.

Writes gzipped-JSONL shards into the run's existing blob store, next to the
golden shards and in the same format, so the export path that already reads
`…/golden/<table>/unit-000123.jsonl.gz` reads these too. Parquet/DuckDB locally,
BigQuery in the cloud — all of which are columnar, which is exactly the right
shape for "sum cost, group by model".

The key layout is doing real work. Because a shard is keyed by
``(run, unit)`` and written on flush, a **retried unit overwrites its own shard
in place**. Double-counted spend is the classic failure of bolt-on cost telemetry,
and here it is impossible by construction rather than by careful bookkeeping.

Rows accumulate in memory between flushes. A unit of 1000 documents at a few
calls each is a few thousand rows — a few hundred KB — against the ~150 MB of
PDFs that unit is already writing, so the buffer and the extra PUT are both noise.
"""

from __future__ import annotations

from docsynth.core.pipeline.golden import encode_shard
from docsynth.core.storage.base import BlobStore
from docsynth.core.usage.base import TABLE, LlmUsage


class ShardUsageSink:
    """LLM usage as one gzipped-JSONL shard per work unit."""

    scheme = "shard"

    def __init__(self, blob: BlobStore, run_id: str, *, table: str = TABLE) -> None:
        self._blob = blob
        self._run_id = run_id
        self._table = table
        self._buffer: list[LlmUsage] = []
        #: Distinguishes flushes within one unit (a worker may flush more than
        #: once), so a second flush cannot clobber the first.
        self._part = 0

    def record(self, usage: LlmUsage) -> None:
        self._buffer.append(usage)

    def flush(self) -> int:
        if not self._buffer:
            return 0
        rows = [u.to_row() for u in self._buffer]
        self._blob.put(self._key(), encode_shard(rows), "application/gzip")
        self._buffer.clear()
        self._part += 1
        return len(rows)

    def close(self) -> None:
        self.flush()

    def _key(self) -> str:
        """``<run>/golden/llm_usage/unit-000123.jsonl.gz`` — the same shape the
        golden tables use, so one export walk finds everything.

        The unit is taken from the buffered rows rather than passed in: every row
        in a flush belongs to the unit that produced it, and reading it from the
        data means the key cannot disagree with the contents. Rows with no unit
        (a catalogue build, which has no work units) go to a ``catalogue`` shard.
        """
        units = {u.unit_index for u in self._buffer if u.unit_index is not None}
        if len(units) == 1:
            stem = f"unit-{units.pop():06d}"
        elif units:
            # Mixed units in one flush: fall back to a part-numbered shard rather
            # than pick one unit's name and lie about the rest.
            stem = f"mixed-{self._part:06d}"
        else:
            stem = f"catalogue-{self._part:06d}"
        return f"{self._run_id}/golden/{self._table}/{stem}.jsonl.gz"
