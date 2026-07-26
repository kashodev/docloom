"""The per-run manifest — a run made self-describing from the bucket alone.

A generation run writes documents and golden shards under its own prefix, but
nothing that says *what* it produced. A consumer — here, a separate Cloud Run app
that reads the corpus and processes it — would otherwise have to list the whole
bucket and hope it saw everything. This module writes the index that removes that
guesswork, and lets the consumer verify completeness with **only bucket access**:
no StateStore, no credentials beyond the blobs.

The shape follows the run's own concurrency, because the manifest has to be
correct under the same distributed, resumable, retryable conditions everything
else survives:

* **A part per unit.** Each worker, on finishing a unit, writes
  ``<run>/manifest/unit-000123.json`` listing that unit's documents and shards
  with a sha256 for each. Written *before* the unit is marked done and keyed by
  unit index, so it overwrites idempotently on a retry, exactly like the shards
  it describes.

* **A root at the run prefix.** ``<run>/manifest.json`` indexes the parts plus
  run-level totals. It is written **only when the run completes**, so its
  presence is the completion signal: a consumer that finds the root can trust the
  corpus is whole. A partial or failed run has parts but no root.

The split keeps the root small — it indexes units, not the million documents —
while the parts hold the per-document detail a consumer needs to fetch and check
individual blobs.

Consuming incrementally, while a run is still generating, is a stronger contract
(the parts would need a documented in-progress state and stable partial reads).
Tracked in TODO.md; today the contract is pull-on-complete.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from docloom.core.storage.base import BlobStore

#: Bumped when the manifest JSON layout changes incompatibly, so a consumer
#: refuses a manifest it does not understand rather than mis-reading it.
MANIFEST_SCHEMA_VERSION = 1

ROOT_NAME = "manifest.json"
_PARTS_DIR = "manifest"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ``prefix`` is a run's storage sub-path. It defaults to the ``run_id`` (the
# historical, flat layout), but a run can nest its output under a shared parent —
# e.g. a multi-slice deploy putting ``<corpus>/<slice>/…`` — by passing a prefix
# distinct from its (still-flat, unique) run id. The state store and golden
# ``run_id`` column are unaffected; only where blobs land changes.
def root_key(prefix: str) -> str:
    return f"{prefix}/{ROOT_NAME}"


def parts_prefix(prefix: str) -> str:
    return f"{prefix}/{_PARTS_DIR}/"


def part_key(prefix: str, unit_index: int) -> str:
    return f"{prefix}/{_PARTS_DIR}/unit-{unit_index:06d}.json"


# ── Per-unit part ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DocumentEntry:
    """One rendered document, so a consumer can fetch and verify it directly."""

    index: int
    key: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class ShardEntry:
    """One golden shard — one table's rows for this unit."""

    table: str
    key: str
    sha256: str
    bytes: int
    rows: int


@dataclass(frozen=True, slots=True)
class UnitManifest:
    """Everything one unit produced. Written as a part, indexed by the root."""

    run_id: str
    unit_index: int
    start_index: int
    count: int
    documents: tuple[DocumentEntry, ...]
    shards: tuple[ShardEntry, ...]

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "run_id": self.run_id,
                "unit_index": self.unit_index,
                "start_index": self.start_index,
                "count": self.count,
                "documents": [
                    {"index": d.index, "key": d.key, "sha256": d.sha256, "bytes": d.bytes}
                    for d in self.documents
                ],
                "shards": [
                    {"table": s.table, "key": s.key, "sha256": s.sha256,
                     "bytes": s.bytes, "rows": s.rows}
                    for s in self.shards
                ],
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> UnitManifest:
        data = json.loads(raw)
        return cls(
            run_id=data["run_id"],
            unit_index=data["unit_index"],
            start_index=data["start_index"],
            count=data["count"],
            documents=tuple(
                DocumentEntry(d["index"], d["key"], d["sha256"], d["bytes"])
                for d in data["documents"]
            ),
            shards=tuple(
                ShardEntry(s["table"], s["key"], s["sha256"], s["bytes"], s["rows"])
                for s in data["shards"]
            ),
        )


# ── Root ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PartRef:
    """The root's pointer to one unit part: enough to find it and its totals,
    without inlining the part's per-document detail into the root."""

    key: str
    unit_index: int
    start_index: int
    count: int
    documents: int
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The self-describing summary of a completed run."""

    run_id: str
    pack: str
    config_id: str
    catalogue_version: str
    created_at: str
    completed_at: str
    total_units: int
    total_documents: int
    table_rows: dict[str, int]
    parts: tuple[PartRef, ...]
    schema_version: int = MANIFEST_SCHEMA_VERSION
    #: The key shapes, so a consumer can reason about layout without parsing keys.
    document_key_pattern: str = "{run_id}/documents/unit-{unit:06d}/{record_id}{ext}"
    shard_key_pattern: str = "{run_id}/golden/{table}/unit-{unit:06d}.jsonl.gz"

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "pack": self.pack,
                "config_id": self.config_id,
                "catalogue_version": self.catalogue_version,
                "created_at": self.created_at,
                "completed_at": self.completed_at,
                "total_units": self.total_units,
                "total_documents": self.total_documents,
                "table_rows": self.table_rows,
                "document_key_pattern": self.document_key_pattern,
                "shard_key_pattern": self.shard_key_pattern,
                "parts": [
                    {"key": p.key, "unit_index": p.unit_index, "start_index": p.start_index,
                     "count": p.count, "documents": p.documents, "bytes": p.bytes,
                     "sha256": p.sha256}
                    for p in self.parts
                ],
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> RunManifest:
        data = json.loads(raw)
        return cls(
            run_id=data["run_id"],
            pack=data["pack"],
            config_id=data["config_id"],
            catalogue_version=data.get("catalogue_version", ""),
            created_at=data.get("created_at", ""),
            completed_at=data.get("completed_at", ""),
            total_units=data["total_units"],
            total_documents=data["total_documents"],
            table_rows=dict(data.get("table_rows", {})),
            parts=tuple(
                PartRef(p["key"], p["unit_index"], p["start_index"], p["count"],
                        p["documents"], p["bytes"], p["sha256"])
                for p in data["parts"]
            ),
            schema_version=int(data.get("schema_version", 0)),
            document_key_pattern=data.get("document_key_pattern",
                                          cls.document_key_pattern),
            shard_key_pattern=data.get("shard_key_pattern", cls.shard_key_pattern),
        )


# ── Writing ─────────────────────────────────────────────────────────────────
def write_unit_manifest(blob: BlobStore, unit: UnitManifest, *, prefix: str = "") -> None:
    """Write one unit's part. Called by the worker after the unit's blobs land
    and before the unit is marked done, so a done unit always has a part."""
    blob.put(part_key(prefix or unit.run_id, unit.unit_index), unit.to_json(), "application/json")


def _part_ref(key: str, raw: bytes, unit: UnitManifest) -> PartRef:
    return PartRef(
        key=key,
        unit_index=unit.unit_index,
        start_index=unit.start_index,
        count=unit.count,
        documents=len(unit.documents),
        bytes=len(raw),
        sha256=sha256_hex(raw),
    )


def write_run_manifest(
    blob: BlobStore,
    *,
    run_id: str,
    pack: str,
    config_id: str,
    total_units: int,
    catalogue_version: str = "",
    created_at: str = "",
    completed_at: str = "",
    storage_prefix: str = "",
) -> RunManifest:
    """Assemble and write the root manifest from the unit parts on the bucket.

    Called only when the run is complete, so every unit part is present — the
    ordering guarantee (part written before the unit is marked done) is what
    makes reading the parts here safe. Idempotent: two workers that both observe
    completion assemble byte-identical roots from the same parts.

    Raises if the parts do not cover ``total_units``: a root that claimed
    completeness over a gap would be worse than none, because a consumer trusts
    it precisely so it does not have to check.
    """
    prefix = storage_prefix or run_id
    parts: list[PartRef] = []
    table_rows: dict[str, int] = {}
    total_documents = 0
    for key in sorted(blob.iter_keys(parts_prefix(prefix))):
        raw = blob.get(key)
        unit = UnitManifest.from_json(raw)
        parts.append(_part_ref(key, raw, unit))
        total_documents += len(unit.documents)
        for shard in unit.shards:
            table_rows[shard.table] = table_rows.get(shard.table, 0) + shard.rows

    seen = {p.unit_index for p in parts}
    if len(seen) != total_units:
        missing = sorted(set(range(total_units)) - seen)
        raise ValueError(
            f"run {run_id!r} has {len(seen)} unit part(s) but {total_units} units — "
            f"refusing to write a root manifest over a gap (missing e.g. {missing[:5]})"
        )

    # The advisory key patterns describe the real layout. Only overridden when the
    # storage prefix differs from the run id, so a flat run's manifest is unchanged.
    patterns: dict[str, str] = {}
    if prefix != run_id:
        patterns = {
            "document_key_pattern": prefix + "/documents/unit-{unit:06d}/{record_id}{ext}",
            "shard_key_pattern": prefix + "/golden/{table}/unit-{unit:06d}.jsonl.gz",
        }
    manifest = RunManifest(
        run_id=run_id,
        pack=pack,
        config_id=config_id,
        catalogue_version=catalogue_version,
        created_at=created_at or _now(),
        completed_at=completed_at or _now(),
        total_units=total_units,
        total_documents=total_documents,
        table_rows=table_rows,
        parts=tuple(sorted(parts, key=lambda p: p.unit_index)),
        **patterns,
    )
    blob.put(root_key(prefix), manifest.to_json(), "application/json")
    return manifest


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── Consuming (the cross-app contract) ──────────────────────────────────────
def read_run_manifest(blob: BlobStore, run_id: str, *, storage_prefix: str = "") -> RunManifest:
    """Load a run's root manifest. Raises ``FileNotFoundError`` if absent — which
    for a consumer means the run is not complete, not that it is malformed."""
    prefix = storage_prefix or run_id
    try:
        raw = blob.get(root_key(prefix))
    except KeyError as exc:
        raise FileNotFoundError(
            f"no run manifest at {root_key(prefix)!r} — the run is incomplete or "
            "does not exist; a completed run always has one"
        ) from exc
    manifest = RunManifest.from_json(raw)
    if manifest.schema_version > MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"run manifest for {run_id!r} is schema version "
            f"{manifest.schema_version}; this docloom understands up to "
            f"{MANIFEST_SCHEMA_VERSION} — upgrade to read it"
        )
    return manifest


def is_complete(blob: BlobStore, run_id: str, *, storage_prefix: str = "") -> bool:
    """Whether a consumer may pull this run — i.e. its root manifest exists."""
    return blob.exists(root_key(storage_prefix or run_id))


@dataclass(slots=True)
class VerificationReport:
    """What a consumer's integrity check found."""

    run_id: str
    parts_checked: int = 0
    blobs_checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_run(blob: BlobStore, run_id: str, *, deep: bool = False) -> VerificationReport:
    """Check a run against its manifest, the way a consumer would before trusting
    it.

    Shallow (default) confirms every unit part exists and matches the sha256 the
    root recorded — cheap, and enough to catch a truncated or missing part.
    ``deep`` additionally re-reads every document and shard blob and checksums it
    against the part, which is thorough but reads the whole corpus, so it is opt
    in.
    """
    report = VerificationReport(run_id=run_id)
    manifest = read_run_manifest(blob, run_id)

    for ref in manifest.parts:
        try:
            raw = blob.get(ref.key)
        except KeyError:
            report.problems.append(f"missing part {ref.key}")
            continue
        report.parts_checked += 1
        if sha256_hex(raw) != ref.sha256:
            report.problems.append(f"part {ref.key} sha256 mismatch")
            continue
        if not deep:
            continue
        unit = UnitManifest.from_json(raw)
        for entry in (*unit.documents, *unit.shards):
            report.blobs_checked += 1
            try:
                blob_bytes = blob.get(entry.key)
            except KeyError:
                report.problems.append(f"missing blob {entry.key}")
                continue
            if sha256_hex(blob_bytes) != entry.sha256:
                report.problems.append(f"blob {entry.key} sha256 mismatch")
    return report


def enumerate_document_keys(blob: BlobStore, run_id: str, *,
                            storage_prefix: str = "") -> Iterable[str]:
    """Yield every document key in the run, from the manifest alone.

    This is the method a consumer uses instead of listing the bucket: the
    manifest is authoritative, so a document that exists but is not listed here
    is not part of the run, and a listed one that is missing is a caught error.
    """
    manifest = read_run_manifest(blob, run_id, storage_prefix=storage_prefix)
    for ref in manifest.parts:
        unit = UnitManifest.from_json(blob.get(ref.key))
        for document in unit.documents:
            yield document.key
