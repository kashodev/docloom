"""LLM usage telemetry: one protocol, backends chosen by URI scheme.

    shard://                      gzipped-JSONL shards beside the golden data (default)
    firestore://project/database  Firestore collection  (pip install 'docloom[gcp]')
    dynamodb://table              DynamoDB table        (pip install 'docloom[aws]')
    null://  ·  off  ·  none      record nothing

**On by default.** A run records what its LLM calls cost unless told not to. The
data is worth roughly 0.05% of the bytes a run already writes and a rounding
error against the spend it measures, so making it opt-in would mostly guarantee
nobody has it when they want to know where the money went.

Costing nothing when there is nothing to cost is what makes that defensible: a
procedural pack (invoices) issues no LLM calls, so it records no rows and writes
no shard. The default is free for the path that does not use an LLM, and present
for the path that does.

The cloud backends are lazy-imported, so the default path needs no cloud SDK.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from docloom.core.storage.base import BlobStore
from docloom.core.usage.base import (
    TABLE,
    LlmUsage,
    MemoryUsageSink,
    NullUsageSink,
    UsageSink,
)
from docloom.core.usage.shard import ShardUsageSink

__all__ = [
    "DEFAULT_USAGE_URI",
    "TABLE",
    "LlmUsage",
    "MemoryUsageSink",
    "NullUsageSink",
    "ShardUsageSink",
    "UsageSink",
    "open_usage_sink",
]

#: What a run uses when nothing is configured: shards beside the golden data.
DEFAULT_USAGE_URI = "shard://"

#: Spellings that mean "record nothing", so disabling does not require knowing
#: that the null sink is spelled as a URI.
_OFF = frozenset({"off", "none", "no", "disabled", "null", "null://"})


def open_usage_sink(
    uri: str | None = None,
    *,
    blob: BlobStore | None = None,
    run_id: str = "",
) -> UsageSink:
    """Construct the usage sink named by ``uri``.

    ``None`` means the default (:data:`DEFAULT_USAGE_URI`), not "off" — see the
    module docstring. ``blob`` and ``run_id`` are needed only by the sharded
    sink, which writes into the run's own storage; passing them costs nothing for
    the other backends.
    """
    if uri is not None and uri.strip().lower() in _OFF:
        return NullUsageSink()

    parsed = urlparse(uri or DEFAULT_USAGE_URI)
    scheme = parsed.scheme or "shard"
    options = dict(parse_qsl(parsed.query))

    if scheme == "shard":
        if blob is None:
            raise ValueError(
                "shard:// usage writes beside the golden data, so it needs the "
                "run's blob store — pass blob=… (or select another backend)"
            )
        return ShardUsageSink(blob, run_id, table=options.get("table", TABLE))

    if scheme == "memory":
        return MemoryUsageSink()

    if scheme == "firestore":
        try:
            from docloom.core.usage.firestore import FirestoreUsageSink
        except ImportError as exc:   # pragma: no cover - depends on the extra
            raise ImportError(
                "firestore:// usage needs the GCP extra — pip install 'docloom[gcp]'"
            ) from exc
        return FirestoreUsageSink(
            project=parsed.netloc,
            database=parsed.path.lstrip("/") or "(default)",
            collection=options.get("collection", TABLE),
        )

    if scheme == "dynamodb":
        from docloom.core.usage.dynamodb import DynamoDbUsageSink

        return DynamoDbUsageSink(
            parsed.netloc or TABLE,
            region=options.get("region"),
            endpoint_url=options.get("endpoint_url"),
        )

    raise ValueError(f"unsupported usage scheme {scheme!r} in {uri!r}")
