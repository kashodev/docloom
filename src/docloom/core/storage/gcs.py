"""Google Cloud Storage blob store (``gs://``).

Same :class:`~docloom.core.storage.base.BlobStore` surface as the local default,
so the generator and exporter do not change when a run moves to GCS.

The SDK is imported lazily inside ``__init__``, for two reasons: the default
local path pulls in no cloud dependency, and an injected client lets the whole
adapter be unit-tested without ``google-cloud-storage`` installed and without
touching a network. A missing extra raises the same actionable message the
factory would.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class GcsBlobStore:
    """Blobs in a GCS bucket, under an optional key prefix."""

    scheme = "gs"

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        if client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:
                raise ImportError(
                    "gs:// storage needs the GCP extra — pip install 'docloom[gcp]'"
                ) from exc
            client = storage.Client()
        self._client = client
        self._bucket_name = bucket
        self._bucket = client.bucket(bucket)
        # Normalise to "" or "prefix/" so joining a key never doubles or drops a
        # slash. All object names are this prefix + the caller's key.
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

    def _name(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        blob = self._bucket.blob(self._name(key))
        blob.upload_from_string(data, content_type=content_type)
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        blob = self._bucket.blob(self._name(key))
        # exists-then-read keeps the missing-key mapping SDK-exception-agnostic;
        # get() is an export-path call, not the hot generation path, so the
        # extra round trip is immaterial.
        if not blob.exists():
            raise KeyError(key)
        return blob.download_as_bytes()

    def exists(self, key: str) -> bool:
        return bool(self._bucket.blob(self._name(key)).exists())

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        full = f"{self._prefix}{prefix}"
        blobs = self._client.list_blobs(self._bucket, prefix=full)
        for name in sorted(b.name for b in blobs):
            yield name[len(self._prefix):]   # strip the store prefix, keep the key

    def uri_for(self, key: str) -> str:
        return f"gs://{self._bucket_name}/{self._name(key)}"
