"""Blob storage: one protocol, backends chosen by URI scheme.

    file:///abs/path      local filesystem (default; no dependencies)
    ./relative/path       shorthand for file://
    gs://bucket/prefix    Google Cloud Storage   (pip install 'docsynth[gcp]')
    s3://bucket/prefix    Amazon S3              (pip install 'docsynth[aws]')

Cloud backends are imported lazily so the default path pulls in no cloud SDK. A
missing extra fails with an actionable message naming the install to run, not an
``ImportError`` deep in a stack trace.
"""

from __future__ import annotations

from urllib.parse import urlparse

from docsynth.core.storage.base import BlobStore
from docsynth.core.storage.local import LocalBlobStore

__all__ = ["BlobStore", "LocalBlobStore", "open_store"]


def open_store(uri: str) -> BlobStore:
    """Construct the blob store named by ``uri``."""
    parsed = urlparse(uri)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        # file:///a/b -> /a/b ; bare ./out or /out -> that path
        return LocalBlobStore(parsed.path if parsed.scheme else uri)

    if scheme == "gs":
        try:
            from docsynth.core.storage.gcs import GcsBlobStore
        except ImportError as exc:
            raise ImportError(
                "gs:// storage needs the GCP extra — pip install 'docsynth[gcp]'"
            ) from exc
        return GcsBlobStore(bucket=parsed.netloc, prefix=parsed.path.lstrip("/"))

    if scheme == "s3":
        try:
            from docsynth.core.storage.s3 import S3BlobStore
        except ImportError as exc:
            raise ImportError(
                "s3:// storage needs the AWS extra — pip install 'docsynth[aws]'"
            ) from exc
        return S3BlobStore(bucket=parsed.netloc, prefix=parsed.path.lstrip("/"))

    raise ValueError(f"unsupported storage scheme {scheme!r} in {uri!r}")
