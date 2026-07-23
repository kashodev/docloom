"""The generation worker — the spine of a run.

One worker drains claimable units until none remain. For each unit it generates
every document in the index range, renders and stores each, accumulates the
golden rows, writes one shard per table, and marks the unit done. A failure
marks the unit failed and the worker moves on — the run's other units are
independent, and a failed unit is retried on the next resume, not in a hot loop.

Every primitive it calls is already built and tested: the atomic claim, the blob
store, the record's ``to_rows``, the renderer, the golden codec. This module is
just the orchestration.

Concurrency has two levels. **Across instances**, many workers run this same loop
against one shared StateStore; the atomic claim guarantees no unit is done twice.
**Within a unit**, documents could render in parallel — the loop is written
sequentially for now (HTML rendering is cheap); the async PDF renderer will make
intra-unit parallelism worthwhile, and it slots in here without changing the
persistence or accounting.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from docloom.core.logging import bound, get_logger
from docloom.core.pipeline.golden import encode_shard
from docloom.core.pipeline.manifest import (
    DocumentEntry,
    ShardEntry,
    UnitManifest,
    sha256_hex,
    write_unit_manifest,
)
from docloom.core.pipeline.renderer import DocumentRenderer
from docloom.core.pipeline.source import DocumentSource
from docloom.core.state.base import StateStore, WorkUnit
from docloom.core.storage.base import BlobStore

_log = get_logger(__name__)


@dataclass(slots=True)
class WorkerStats:
    """What one ``run()`` accomplished."""

    units_completed: int = 0
    units_failed: int = 0
    documents_written: int = 0
    failures: list[tuple[int, str]] = field(default_factory=list)  # (unit_index, error)


class GenerationWorker:
    """Drains work units for one run, generating and persisting documents."""

    def __init__(
        self,
        *,
        run_id: str,
        source: DocumentSource,
        renderer: DocumentRenderer,
        blob: BlobStore,
        state: StateStore,
    ) -> None:
        self._run_id = run_id
        self._source = source
        self._renderer = renderer
        self._blob = blob
        self._state = state

    def run(self) -> WorkerStats:
        """Claim and process units until the pool is empty."""
        stats = WorkerStats()
        while (unit := self._state.claim_next_unit(self._run_id)) is not None:
            self._process(unit, stats)
        return stats

    def _process(self, unit: WorkUnit, stats: WorkerStats) -> None:
        with bound(unit=unit.unit_index, start_index=unit.start_index, count=unit.count):
            start = time.monotonic()
            try:
                written = self._generate_unit(unit)
            except Exception as exc:
                # A failed unit is recorded and left out of the pool; the worker
                # continues with the next. Resume retries it. Re-raising here
                # would abort the whole run over one bad document. It is logged —
                # a swallowed failure that only lived in a StateStore row is the
                # kind of silent loss this project has been bitten by.
                self._state.fail_unit(self._run_id, unit.unit_index, repr(exc))
                stats.units_failed += 1
                stats.failures.append((unit.unit_index, repr(exc)))
                _log.warning("unit failed", attempts=unit.attempts + 1, error=repr(exc),
                             exc_info=exc)
                return
            self._state.complete_unit(self._run_id, unit.unit_index)
            stats.units_completed += 1
            stats.documents_written += written
            _log.info("unit completed", documents=written,
                      elapsed_s=round(time.monotonic() - start, 2))

    def _generate_unit(self, unit: WorkUnit) -> int:
        """Generate, render, persist, and shard one unit. Returns doc count.

        Documents are written first, then the golden shards, then the unit's
        manifest part, then the unit is marked done by the caller. That order is
        load-bearing: a shard is never recorded for documents that were not
        stored, and a *completed* unit always has a manifest part, which is what
        lets the root manifest trust the parts at run completion.
        """
        rows_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        documents: list[DocumentEntry] = []

        for index in range(unit.start_index, unit.end_index):
            record = self._source.generate(self._run_id, index)
            document = self._renderer.render(record)
            key = self._document_key(unit, record.record_id, document.extension)
            self._blob.put(key, document.data, document.content_type)
            documents.append(
                DocumentEntry(index=index, key=key,
                              sha256=sha256_hex(document.data), bytes=len(document.data))
            )
            for table, rows in record.to_rows().items():
                rows_by_table[table].extend(rows)

        shards: list[ShardEntry] = []
        for table, rows in rows_by_table.items():
            key = self._shard_key(unit, table)
            encoded = encode_shard(rows)
            self._blob.put(key, encoded, "application/gzip")
            shards.append(
                ShardEntry(table=table, key=key, sha256=sha256_hex(encoded),
                           bytes=len(encoded), rows=len(rows))
            )

        # The unit's part lands before the caller marks the unit done, so the
        # root manifest — assembled at completion — never reads a done unit that
        # has no part.
        write_unit_manifest(
            self._blob,
            UnitManifest(
                run_id=self._run_id, unit_index=unit.unit_index,
                start_index=unit.start_index, count=unit.count,
                documents=tuple(documents), shards=tuple(shards),
            ),
        )
        return unit.count

    # ── Deterministic key layout ────────────────────────────────────────────
    # Sharded by unit index so a run's blobs group naturally and a retry (same
    # unit, same deterministic content) overwrites in place.
    def _document_key(self, unit: WorkUnit, record_id: str, extension: str) -> str:
        return f"{self._run_id}/documents/unit-{unit.unit_index:06d}/{record_id}{extension}"

    def _shard_key(self, unit: WorkUnit, table: str) -> str:
        return f"{self._run_id}/golden/{table}/unit-{unit.unit_index:06d}.jsonl.gz"
