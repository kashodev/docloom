"""Firestore usage sink (``firestore://project/database``).

For deployments already using ``firestore://`` for run state and wanting usage in
the same place. Written in batches, with a **deterministic document id** per row
so a retried unit overwrites rather than duplicating — the same
no-double-counting property the sharded sink gets from its key layout.

**Know what this is for.** Firestore is an OLTP document store: excellent at point
reads and writes, poor at "sum cost grouped by model over 30 days", which is what
this data is actually for. Its aggregation queries bill per index entry read and
do not suit multi-million-document scans. Prefer :mod:`docloom.core.usage.shard`
for the fact table and use this when operational convenience — one datastore, one
set of credentials — outweighs query cost, or at reduced granularity.

The SDK is lazy-imported and the client injectable, so this module loads without
``google-cloud-firestore`` and its mapping is unit-tested without it.
"""

from __future__ import annotations

from typing import Any

from docloom.core.usage.base import TABLE, LlmUsage

#: Firestore has no Decimal type, and float would lose the sub-cent tail this
#: data exists to track — so cost is stored as an exact decimal *string* and
#: parsed back on read. Also kept as a float for Firestore-side aggregation,
#: which is lossy and labelled as such.
_COST_STRING = "cost_usd"
_COST_FLOAT = "cost_usd_approx"


def usage_to_doc(usage: LlmUsage) -> dict[str, Any]:
    """Row → Firestore document. Cost is exact as a string, approximate as a
    float (Firestore cannot aggregate strings, and cannot store Decimal)."""
    row = usage.to_row()
    # format(..., "f") not str(): str gives scientific notation for small values
    # ("4E-7"), which is exact but hostile to anything parsing the column.
    row[_COST_STRING] = format(usage.cost_usd, "f")
    row[_COST_FLOAT] = float(usage.cost_usd)
    return row


def usage_doc_id(usage: LlmUsage, sequence: int) -> str:
    """A deterministic id, so a re-run overwrites instead of double-counting.

    Built from run/unit/sequence rather than a random id: the same unit replayed
    produces the same ids and therefore the same documents.
    """
    unit = "cat" if usage.unit_index is None else f"{usage.unit_index:06d}"
    return f"{usage.run_id}__{unit}__{sequence:08d}"


class FirestoreUsageSink:
    """LLM usage rows in a Firestore collection."""

    scheme = "firestore"
    #: Firestore caps a batched write at 500 operations.
    _BATCH_LIMIT = 500

    def __init__(
        self,
        project: str,
        database: str = "(default)",
        *,
        client: Any | None = None,
        collection: str = TABLE,
    ) -> None:
        if client is None:
            try:
                from google.cloud import firestore
            except ImportError as exc:
                raise ImportError(
                    "firestore:// usage needs the GCP extra — pip install 'docloom[gcp]'"
                ) from exc
            client = firestore.Client(project=project, database=database)
        self._client = client
        self._collection = collection
        self._buffer: list[LlmUsage] = []
        self._written = 0

    def record(self, usage: LlmUsage) -> None:
        self._buffer.append(usage)

    def flush(self) -> int:
        if not self._buffer:
            return 0
        col = self._client.collection(self._collection)
        written = 0
        for chunk in self._chunks(self._buffer, self._BATCH_LIMIT):
            batch = self._client.batch()
            for usage in chunk:
                doc_id = usage_doc_id(usage, self._written + written)
                batch.set(col.document(doc_id), usage_to_doc(usage))
                written += 1
            batch.commit()
        self._buffer.clear()
        self._written += written
        return written

    def close(self) -> None:
        self.flush()

    @staticmethod
    def _chunks(rows: list[LlmUsage], size: int) -> list[list[LlmUsage]]:
        return [rows[i:i + size] for i in range(0, len(rows), size)]
