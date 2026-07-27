"""GCS and S3 blob-store tests, via dict-backed fake clients.

The fakes are faithful for put/get/exists/list, so these exercise the adapters'
real logic — prefix joining, key stripping in ``iter_keys``, URI building, and
the missing-key → ``KeyError`` mapping — without a cloud account or the SDKs
installed. Parametrised over both backends so they stay behaviourally identical.
"""

from __future__ import annotations

import pytest

from docsynth.core.storage.base import BlobStore
from docsynth.core.storage.gcs import GcsBlobStore
from docsynth.core.storage.s3 import S3BlobStore
from tests.fakes import FakeGcsClient, FakeS3Client


def make_store(kind: str, prefix: str = ""):
    if kind == "gcs":
        return GcsBlobStore("my-bucket", prefix, client=FakeGcsClient())
    return S3BlobStore("my-bucket", prefix, client=FakeS3Client())


BACKENDS = ["gcs", "s3"]


@pytest.mark.parametrize("kind", BACKENDS)
def test_conforms_to_protocol(kind: str) -> None:
    assert isinstance(make_store(kind), BlobStore)


@pytest.mark.parametrize("kind", BACKENDS)
def test_put_get_roundtrip(kind: str) -> None:
    store = make_store(kind)
    uri = store.put("run_x/doc/inv_1.pdf", b"%PDF...", "application/pdf")
    assert uri == f"{kind if kind == 's3' else 'gs'}://my-bucket/run_x/doc/inv_1.pdf"
    assert store.get("run_x/doc/inv_1.pdf") == b"%PDF..."
    assert store.exists("run_x/doc/inv_1.pdf")


@pytest.mark.parametrize("kind", BACKENDS)
def test_missing_key_raises_keyerror(kind: str) -> None:
    with pytest.raises(KeyError):
        make_store(kind).get("absent")


@pytest.mark.parametrize("kind", BACKENDS)
def test_exists_is_false_for_absent_key(kind: str) -> None:
    assert make_store(kind).exists("absent") is False


@pytest.mark.parametrize("kind", BACKENDS)
def test_prefix_is_applied_to_object_names_but_hidden_from_keys(kind: str) -> None:
    """A store prefix scopes the bucket; callers still use bare keys."""
    store = make_store(kind, prefix="docsynth/v1")
    store.put("a/b.txt", b"x")
    # The key the caller sees is bare...
    assert list(store.iter_keys()) == ["a/b.txt"]
    # ...but the URI shows the prefix was applied to the object name.
    assert store.uri_for("a/b.txt").endswith("my-bucket/docsynth/v1/a/b.txt")


@pytest.mark.parametrize("kind", BACKENDS)
def test_iter_keys_is_ordered_and_prefix_scoped(kind: str) -> None:
    store = make_store(kind)
    for key in ["run_x/golden/shard-2.jsonl", "run_x/golden/shard-1.jsonl",
                "run_y/golden/shard-1.jsonl"]:
        store.put(key, b"{}")
    assert list(store.iter_keys("run_x/golden/")) == [
        "run_x/golden/shard-1.jsonl",
        "run_x/golden/shard-2.jsonl",
    ]


@pytest.mark.parametrize("kind", BACKENDS)
def test_empty_prefix_lists_everything(kind: str) -> None:
    store = make_store(kind)
    store.put("a.txt", b"1")
    store.put("b/c.txt", b"2")
    assert list(store.iter_keys()) == ["a.txt", "b/c.txt"]
