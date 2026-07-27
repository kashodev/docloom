"""GCS blob-store integration test against fake-gcs-server.

The pure adapter logic (prefix joining, key stripping, URI shape) is covered
without a network in test_storage. This exercises the *real* google-cloud-storage
client against an emulator, so the SDK calls in GcsBlobStore — upload, download,
exists, list — are proven, not mocked.

Gated: skipped unless STORAGE_EMULATOR_HOST is set. To run it:

    docker run -d -p 4443:4443 fsouza/fake-gcs-server -scheme http \
        -public-host localhost:4443
    STORAGE_EMULATOR_HOST=http://localhost:4443 \
        pip install google-cloud-storage && pytest tests/test_gcs_emulator.py
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("STORAGE_EMULATOR_HOST"),
    reason="needs fake-gcs-server (set STORAGE_EMULATOR_HOST)",
)


@pytest.fixture
def bucket_store():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    from docsynth.core.storage.gcs import GcsBlobStore

    client = storage.Client(project="docsynth-test", credentials=AnonymousCredentials())
    name = f"docsynth-{uuid.uuid4().hex[:12]}"
    client.create_bucket(name)
    return GcsBlobStore(name, prefix="runs", client=client)


def test_put_get_roundtrip(bucket_store) -> None:
    uri = bucket_store.put("r/pdf/inv_1.pdf", b"%PDF-1.7 body", "application/pdf")
    assert uri.startswith("gs://") and uri.endswith("runs/r/pdf/inv_1.pdf")
    assert bucket_store.get("r/pdf/inv_1.pdf") == b"%PDF-1.7 body"


def test_exists_and_missing_key_raises(bucket_store) -> None:
    assert bucket_store.exists("r/a.bin") is False
    bucket_store.put("r/a.bin", b"x")
    assert bucket_store.exists("r/a.bin") is True
    with pytest.raises(KeyError):
        bucket_store.get("r/does-not-exist.bin")


def test_iter_keys_is_sorted_and_prefix_stripped(bucket_store) -> None:
    for key in ("r/golden/0002.gz", "r/golden/0000.gz", "r/golden/0001.gz", "r/pdf/x.pdf"):
        bucket_store.put(key, b"z")
    # Keys come back store-prefix-stripped, lexicographically ordered, filtered.
    assert list(bucket_store.iter_keys("r/golden/")) == [
        "r/golden/0000.gz", "r/golden/0001.gz", "r/golden/0002.gz",
    ]


def test_overwrite_on_retry_is_idempotent(bucket_store) -> None:
    bucket_store.put("r/pdf/inv.pdf", b"first")
    bucket_store.put("r/pdf/inv.pdf", b"second")   # a retry re-writes the same key
    assert bucket_store.get("r/pdf/inv.pdf") == b"second"
