"""Per-run manifest tests — the cross-app contract.

A separate consumer (a Cloud Run app that reads the corpus and processes it) uses
the manifest to enumerate and verify a run with *only bucket access*. So the load
-bearing properties are: a completed run is fully described and checksum-verified
from the bucket alone; a partial or failed run has **no** root manifest, so a
consumer never mistakes it for complete; and the whole thing survives resume and
retry, because generation does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import docloom.packs  # noqa: F401 - registers the invoice pack
from docloom.core import get_pack
from docloom.core.enums import RunState, WorkUnitState
from docloom.core.pipeline import HtmlRenderer, create_run, resume_run, work_run
from docloom.core.pipeline.manifest import (
    enumerate_document_keys,
    is_complete,
    read_run_manifest,
    root_key,
    verify_run,
)
from docloom.core.pipeline.renderer import RenderedDocument
from docloom.core.pipeline.source import DocumentSource
from docloom.core.state.sqlite import SqliteStateStore
from docloom.core.storage.local import LocalBlobStore


def _run(tmp_path: Path, *, total: int = 12, unit_size: int = 4, run_id: str = "r",
         storage_prefix: str = ""):  # noqa: ANN202
    blob = LocalBlobStore(str(tmp_path / "blobs"))
    state = SqliteStateStore(tmp_path / "runs.db")
    source = get_pack("invoice").default_source(max_line_items=4)
    create_run(state, run_id=run_id, pack="invoice", config_id="cfg",
               total=total, unit_size=unit_size)
    stats = work_run(state, run_id=run_id, source=source,
                     renderer=HtmlRenderer(get_pack("invoice")), blob=blob,
                     storage_prefix=storage_prefix)
    return blob, state, stats


# ── A completed run is fully described ──────────────────────────────────────
def test_a_completed_run_writes_a_root_manifest(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=12, unit_size=4)
    assert is_complete(blob, "r")
    manifest = read_run_manifest(blob, "r")
    assert manifest.total_units == 3
    assert manifest.total_documents == 12
    assert len(manifest.parts) == 3


def test_the_manifest_enumerates_every_document_from_the_bucket_alone(tmp_path: Path) -> None:
    """The consumer contract: reconstruct the full document list without listing
    the bucket."""
    blob, _, _ = _run(tmp_path, total=12, unit_size=4)

    from_manifest = set(enumerate_document_keys(blob, "r"))
    from_bucket = {k for k in blob.iter_keys("r/documents/") if k.endswith(".html")}
    assert from_manifest == from_bucket
    assert len(from_manifest) == 12


def test_the_manifest_records_golden_row_totals(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=8, unit_size=4)
    manifest = read_run_manifest(blob, "r")
    assert manifest.table_rows["invoices"] == 8
    assert manifest.table_rows["line_items"] > 0
    assert manifest.pack == "invoice"
    assert manifest.catalogue_version == "seed-1"


def test_verify_passes_for_an_intact_run(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=12, unit_size=4)
    report = verify_run(blob, "r", deep=True)
    assert report.ok, report.problems
    assert report.parts_checked == 3
    assert report.blobs_checked > 12   # documents + shards


# ── Integrity is real ───────────────────────────────────────────────────────
def test_a_tampered_document_is_caught_by_deep_verify(tmp_path: Path) -> None:
    """A truncated or swapped blob must be a caught error, not a surprise for the
    consumer downstream."""
    blob, _, _ = _run(tmp_path, total=8, unit_size=4)
    doc = next(k for k in blob.iter_keys("r/documents/") if k.endswith(".html"))
    blob.put(doc, b"corrupted", "text/html")

    shallow = verify_run(blob, "r", deep=False)
    assert shallow.ok, "the part is intact; only the blob changed"
    deep = verify_run(blob, "r", deep=True)
    assert not deep.ok
    assert any("sha256 mismatch" in p for p in deep.problems)


def test_a_tampered_part_is_caught_shallowly(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=8, unit_size=4)
    part = next(k for k in blob.iter_keys("r/manifest/"))
    blob.put(part, b'{"unit_index": 0}', "application/json")
    report = verify_run(blob, "r")
    assert not report.ok
    assert any("sha256 mismatch" in p for p in report.problems)


# ── A partial run is never mistaken for complete ────────────────────────────
class _FlakyRenderer(HtmlRenderer):
    """Fails one unit's documents so the run cannot complete."""

    def render(self, record) -> RenderedDocument:  # noqa: ANN001
        if record.invoice_index >= 8:
            raise RuntimeError("boom")
        return super().render(record)


def test_a_failed_run_has_no_root_manifest(tmp_path: Path) -> None:
    """The completion signal: no root means 'do not pull yet', not 'malformed'."""
    blob = LocalBlobStore(str(tmp_path / "blobs"))
    state = SqliteStateStore(tmp_path / "runs.db")
    source = get_pack("invoice").default_source(max_line_items=4)
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=12, unit_size=4)
    work_run(state, run_id="r", source=source,
             renderer=_FlakyRenderer(get_pack("invoice")), blob=blob)

    assert state.get_run("r").state is not RunState.COMPLETED   # type: ignore[union-attr]
    assert not is_complete(blob, "r")
    with pytest.raises(FileNotFoundError, match="run is incomplete"):
        read_run_manifest(blob, "r")
    # The units that DID finish still wrote their parts — a resume completes it.
    assert any(blob.iter_keys("r/manifest/"))


def test_a_resume_completes_the_manifest(tmp_path: Path) -> None:
    blob = LocalBlobStore(str(tmp_path / "blobs"))
    state = SqliteStateStore(tmp_path / "runs.db")
    pack = get_pack("invoice")
    create_run(state, run_id="r", pack="invoice", config_id="cfg", total=12, unit_size=4)
    work_run(state, run_id="r", source=pack.default_source(max_line_items=4),
             renderer=_FlakyRenderer(pack), blob=blob)
    assert not is_complete(blob, "r")

    resume_run(state, "r")
    work_run(state, run_id="r", source=pack.default_source(max_line_items=4),
             renderer=HtmlRenderer(pack), blob=blob)

    assert is_complete(blob, "r")
    manifest = read_run_manifest(blob, "r")
    assert manifest.total_documents == 12
    assert verify_run(blob, "r", deep=True).ok


# ── Idempotence and gaps ────────────────────────────────────────────────────
def test_writing_the_manifest_twice_is_byte_identical(tmp_path: Path) -> None:
    """Several finishing workers may all reach the write; they must agree."""
    blob, state, _ = _run(tmp_path, total=8, unit_size=4)
    first = blob.get(root_key("r"))
    # A second worker draining an already-complete run re-writes the root.
    source = get_pack("invoice").default_source(max_line_items=4)
    work_run(state, run_id="r", source=source,
             renderer=HtmlRenderer(get_pack("invoice")), blob=blob)
    assert blob.get(root_key("r")) == first


def test_the_root_refuses_to_claim_completeness_over_a_gap(tmp_path: Path) -> None:
    """A root manifest is trusted precisely so a consumer need not check for
    gaps, so writing one over a missing unit part must fail loudly."""
    from docloom.core.pipeline.manifest import write_run_manifest

    blob, _, _ = _run(tmp_path, total=8, unit_size=4)
    part = next(k for k in blob.iter_keys("r/manifest/"))
    # Simulate a lost part (a worker that crashed after its docs but before its
    # part), then force a re-assembly over the gap.
    (tmp_path / "blobs" / part).unlink()
    with pytest.raises(ValueError, match="refusing to write a root manifest over a gap"):
        write_run_manifest(blob, run_id="r", pack="invoice", config_id="cfg",
                           total_units=2)


# ── Export is unaffected ────────────────────────────────────────────────────
def test_manifest_keys_do_not_leak_into_the_golden_export(tmp_path: Path) -> None:
    """Export walks {run}/golden/; the manifest lives elsewhere and must not be
    mistaken for a shard."""
    from docloom.core.pipeline import export_run
    from docloom.core.sinks import open_sink

    blob, _, _ = _run(tmp_path, total=8, unit_size=4)
    sink = open_sink(f"duckdb:///{tmp_path}/g.db")
    stats = export_run("r", blob, sink)
    assert set(stats.tables) == {"invoices", "line_items"}
    assert "manifest" not in stats.tables


# ── Nested layout (storage_prefix) ──────────────────────────────────────────
def test_a_nested_run_writes_everything_under_the_prefix(tmp_path: Path) -> None:
    """A run can nest its blobs under a shared parent; the run id stays flat."""
    blob, _, _ = _run(tmp_path, total=8, unit_size=4, storage_prefix="corpus/anchor")
    # documents, golden and the manifest land under the nested prefix …
    assert any(k.startswith("corpus/anchor/documents/") for k in blob.iter_keys("corpus/"))
    assert any(k.startswith("corpus/anchor/golden/") for k in blob.iter_keys("corpus/"))
    assert blob.exists("corpus/anchor/manifest.json")
    # … and nothing lands at the flat run-id path
    assert list(blob.iter_keys("r/")) == []


def test_a_nested_run_manifest_reads_back_with_the_prefix(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=8, unit_size=4, storage_prefix="corpus/anchor")
    assert is_complete(blob, "r", storage_prefix="corpus/anchor")
    m = read_run_manifest(blob, "r", storage_prefix="corpus/anchor")
    assert m.run_id == "r" and m.total_documents == 8           # run id stays flat
    assert m.document_key_pattern.startswith("corpus/anchor/documents/")
    keys = set(enumerate_document_keys(blob, "r", storage_prefix="corpus/anchor"))
    assert keys and all(k.startswith("corpus/anchor/documents/") for k in keys)


def test_export_reads_from_the_nested_prefix(tmp_path: Path) -> None:
    from docloom.core.pipeline import export_run
    from docloom.core.sinks import ParquetSink
    blob, _, _ = _run(tmp_path, total=8, unit_size=4, storage_prefix="corpus/anchor")
    stats = export_run("r", blob, ParquetSink(tmp_path / "export"), storage_prefix="corpus/anchor")
    assert stats.total_rows > 0 and "invoices" in stats.tables


def test_the_default_flat_layout_is_unchanged(tmp_path: Path) -> None:
    blob, _, _ = _run(tmp_path, total=8, unit_size=4)          # no prefix
    assert blob.exists("r/manifest.json")
    m = read_run_manifest(blob, "r")
    assert m.document_key_pattern == "{run_id}/documents/unit-{unit:06d}/{record_id}{ext}"


# ── Run group (aggregate root over a multi-slice run) ───────────────────────
def _multi(tmp_path: Path, group: str, slices: tuple[str, ...]):
    """Generate several nested slices of one run into a shared bucket."""
    blob = LocalBlobStore(str(tmp_path / "blobs"))
    state = SqliteStateStore(tmp_path / "runs.db")
    pack = get_pack("invoice")
    for s in slices:
        create_run(state, run_id=f"{group}-{s}", pack="invoice", config_id=s,
                   total=8, unit_size=4)
        work_run(state, run_id=f"{group}-{s}", source=pack.default_source(max_line_items=4),
                 renderer=HtmlRenderer(pack), blob=blob, storage_prefix=f"{group}/{s}")
    return blob


def test_group_manifest_aggregates_the_slices(tmp_path: Path) -> None:
    from docloom.core.pipeline import (
        read_group_manifest,
        read_run_manifest,
        write_group_manifest,
    )
    blob = _multi(tmp_path, "corpus", ("anchor", "clean"))
    gm = write_group_manifest(blob, group_id="corpus", slice_names=("anchor", "clean"))
    # one aggregate root at the run's top level, summing the slices
    assert blob.exists("corpus/manifest.json")
    assert gm.total_documents == 16 and len(gm.slices) == 2
    assert {s.name for s in gm.slices} == {"anchor", "clean"}
    assert gm.table_rows["invoices"] == 16
    # per-slice roots are preserved untouched
    assert blob.exists("corpus/anchor/manifest.json")
    slice_root = read_run_manifest(blob, "corpus-anchor", storage_prefix="corpus/anchor")
    assert slice_root.total_documents == 8
    # reads back as a group
    assert read_group_manifest(blob, "corpus").group_id == "corpus"


def test_group_manifest_discovers_slices_without_names(tmp_path: Path) -> None:
    from docloom.core.pipeline import write_group_manifest
    blob = _multi(tmp_path, "corpus", ("anchor", "clean", "handwritten"))
    gm = write_group_manifest(blob, group_id="corpus")           # discover
    assert len(gm.slices) == 3 and gm.total_documents == 24


def test_group_manifest_refuses_a_missing_named_slice(tmp_path: Path) -> None:
    from docloom.core.pipeline import write_group_manifest
    blob = _multi(tmp_path, "corpus", ("anchor",))               # 'clean' never ran
    with pytest.raises(FileNotFoundError, match="incomplete"):
        write_group_manifest(blob, group_id="corpus", slice_names=("anchor", "clean"))


def test_a_slice_root_is_not_mistaken_for_a_group(tmp_path: Path) -> None:
    from docloom.core.pipeline import read_group_manifest
    blob = _multi(tmp_path, "corpus", ("anchor",))
    with pytest.raises(ValueError, match="not a run-group"):
        read_group_manifest(blob, "corpus/anchor")              # that key is a RunManifest


def test_finalize_run_command_writes_the_group_manifest(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from docloom.cli import app
    blob = _multi(tmp_path, "corpus", ("anchor", "clean"))
    res = CliRunner().invoke(app, ["finalize-run", "--run-id", "corpus",
                                   "--storage", str(tmp_path / "blobs")])
    assert res.exit_code == 0, res.output
    assert "2 slice(s)" in res.output and blob.exists("corpus/manifest.json")
