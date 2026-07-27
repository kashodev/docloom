"""Blob-storage tests. Focused on the local default and the scheme factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from docsynth.core.storage import LocalBlobStore, open_store


def test_put_get_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    uri = store.put("run_x/pdf/inv_1.pdf", b"%PDF-1.7 ...", "application/pdf")
    assert uri.startswith("file://")
    assert store.get("run_x/pdf/inv_1.pdf") == b"%PDF-1.7 ..."
    assert store.exists("run_x/pdf/inv_1.pdf")


def test_overwrite_on_retry_is_idempotent(tmp_path: Path) -> None:
    """Deterministic generation rewrites identical bytes on retry."""
    store = LocalBlobStore(tmp_path)
    store.put("k", b"v1")
    store.put("k", b"v2")
    assert store.get("k") == b"v2"


def test_missing_key_raises_keyerror(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        LocalBlobStore(tmp_path).get("nope")


def test_iter_keys_is_ordered_and_prefix_scoped(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    for key in ["run_x/golden/shard-2.jsonl", "run_x/golden/shard-1.jsonl",
                "run_y/golden/shard-1.jsonl"]:
        store.put(key, b"{}")
    got = list(store.iter_keys("run_x/golden/"))
    assert got == ["run_x/golden/shard-1.jsonl", "run_x/golden/shard-2.jsonl"]


def test_key_escaping_the_root_is_refused(tmp_path: Path) -> None:
    """A synthetic-document tool must never write outside its output dir."""
    store = LocalBlobStore(tmp_path / "out")
    with pytest.raises(ValueError, match="escapes"):
        store.put("../evil.txt", b"x")


def test_partial_writes_leave_no_tmp_files(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("a/b/c.bin", b"data")
    assert not list(tmp_path.rglob("*.tmp"))


def test_factory_defaults_to_local(tmp_path: Path) -> None:
    store = open_store(str(tmp_path))
    assert isinstance(store, LocalBlobStore)


def test_factory_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="unsupported storage scheme"):
        open_store("wat://bucket/x")


def test_cloud_schemes_give_actionable_error() -> None:
    """A missing extra names the install to run, not a bare ImportError."""
    with pytest.raises(ImportError, match=r"docsynth\[gcp\]"):
        open_store("gs://bucket/prefix")
    with pytest.raises(ImportError, match=r"docsynth\[aws\]"):
        open_store("s3://bucket/prefix")
