"""Filesystem blob store — the zero-dependency default.

Makes ``docsynth`` runnable with no cloud account: point ``storage`` at a
``file://`` URI (or a bare path) and rendered documents and golden shards land
under a directory. Writes are atomic — temp file then ``os.replace`` — so a
crashed or concurrent run never leaves a half-written PDF that a later step
would mistake for complete.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


class LocalBlobStore:
    """A :class:`~docsynth.core.storage.base.BlobStore` backed by a directory."""

    scheme = "file"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Contain every key within the root. A key escaping via ``..`` is a
        # programming error, but a synthetic-document tool must never write
        # outside its output directory, so it is refused rather than trusted.
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"key {key!r} escapes the storage root")
        return target

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)   # atomic within a filesystem
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(key) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        base = self._path(prefix) if prefix else self._root
        root = base if base.is_dir() else base.parent
        for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix != ".tmp"):
            key = path.relative_to(self._root).as_posix()
            if key.startswith(prefix):
                yield key

    def uri_for(self, key: str) -> str:
        return (self._root / key).as_uri()
