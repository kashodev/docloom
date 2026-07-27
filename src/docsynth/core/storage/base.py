"""Blob storage — where rendered documents and golden shards land.

One narrow protocol, several backends, selected by URI scheme. Calling code
writes ``store.put("run_x/pdf/inv_1.pdf", data, "application/pdf")`` and never
learns whether that became a file on disk, an object in GCS, or a key in S3.

Keys are POSIX-style relative paths (forward slashes, no leading slash). The
store owns the mapping from key to physical location and returns a fully
qualified URI from :meth:`put` so callers can record where a blob actually went.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    """A flat key → bytes store."""

    scheme: str

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Write ``data`` at ``key``. Returns the blob's fully qualified URI.

        Overwrites an existing key: generation is deterministic per
        ``(run_id, index)``, so a retry writes byte-identical content and
        overwrite-on-retry is exactly the idempotent behaviour wanted.
        """
        ...

    def get(self, key: str) -> bytes:
        """Read the blob at ``key``. Raises ``KeyError`` if absent."""
        ...

    def exists(self, key: str) -> bool:
        ...

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        """Yield keys under ``prefix`` in lexicographic order.

        Export walks the golden shards this way; ordering makes runs
        reproducible and lets a consumer resume from a known key.
        """
        ...

    def uri_for(self, key: str) -> str:
        """The fully qualified URI a key maps to, without touching storage."""
        ...
