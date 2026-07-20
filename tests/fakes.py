"""Dict-backed fakes for the cloud SDKs.

Faithful enough that the storage tests are real behavioural coverage: put/get/
exists/list are simple key→bytes operations, so a dict fake exercises the
adapter's actual logic (prefix joining, key stripping, URI building, missing-key
mapping). The BigQuery fake records executed SQL so the external-table DDL can be
asserted. Nothing here pretends to reproduce Firestore's transaction semantics —
that is the platform's guarantee, tested against the emulator, not here.
"""

from __future__ import annotations

from typing import Any


# ── Google Cloud Storage ────────────────────────────────────────────────────
class FakeGcsBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self.name] = data

    def download_as_bytes(self) -> bytes:
        return self._store[self.name]

    def exists(self) -> bool:
        return self.name in self._store


class FakeGcsBucket:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def blob(self, name: str) -> FakeGcsBlob:
        return FakeGcsBlob(self._store, name)


class FakeGcsClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeGcsBucket:
        return FakeGcsBucket(self.store, name)

    def list_blobs(self, bucket: Any, prefix: str = "") -> list[FakeGcsBlob]:
        return [FakeGcsBlob(self.store, n) for n in self.store if n.startswith(prefix)]


# ── Amazon S3 ───────────────────────────────────────────────────────────────
class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:  # noqa: N803
        self.store[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.store:
            raise KeyError(Key)   # name is in S3BlobStore._NOT_FOUND
        return {"Body": _Body(self.store[Key])}

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.store:
            raise KeyError(Key)
        return {}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", **_: Any) -> dict[str, Any]:  # noqa: N803
        contents = [{"Key": k} for k in self.store if k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}


# ── BigQuery ────────────────────────────────────────────────────────────────
class _FakeQueryJob:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def result(self) -> list[Any]:
        return self._rows


class FakeBigQueryClient:
    """Records executed SQL; that is all the DDL/register path needs to assert."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query(self, sql: str) -> _FakeQueryJob:
        self.executed.append(sql)
        return _FakeQueryJob([])
