"""Amazon S3 blob store (``s3://``).

Rounds out the "interoperable across clouds, or none" story: the same
:class:`~docloom.core.storage.base.BlobStore` surface over an S3 bucket. SDK is
lazy-imported and the client is injectable, so the module imports and tests
without ``boto3`` installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# Exception class names that mean "the key is not there". Matched by name so the
# adapter need not import botocore, keeping it importable without the AWS extra.
_NOT_FOUND = {"NoSuchKey", "NoSuchBucket", "ClientError", "KeyError", "404"}


class S3BlobStore:
    """Blobs in an S3 bucket, under an optional key prefix."""

    scheme = "s3"

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "s3:// storage needs the AWS extra — pip install 'docloom[aws]'"
                ) from exc
            client = boto3.client("s3")
        self._client = client
        self._bucket = bucket
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

    def _name(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(
            Bucket=self._bucket, Key=self._name(key), Body=data, ContentType=content_type
        )
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._name(key))
        except Exception as exc:  # narrow to missing-key; re-raise anything else
            if type(exc).__name__ in _NOT_FOUND:
                raise KeyError(key) from exc
            raise
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._name(key))
            return True
        except Exception as exc:
            if type(exc).__name__ in _NOT_FOUND:
                return False
            raise

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        full = f"{self._prefix}{prefix}"
        names: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": full}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            names.extend(obj["Key"] for obj in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        for name in sorted(names):
            yield name[len(self._prefix):]

    def uri_for(self, key: str) -> str:
        return f"s3://{self._bucket}/{self._name(key)}"
