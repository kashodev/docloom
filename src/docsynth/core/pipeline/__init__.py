"""Generation pipeline — the spine that turns tested components into a run.

    plan → create → [claim → generate → render → persist → shard → complete]* → done

Document-agnostic: the worker knows the loop; a pack supplies the
:class:`DocumentSource` (how to invent a record) and the render kernel supplies
the :class:`DocumentRenderer` (record → bytes). Storage, state, and the golden
codec are the kernel's.
"""

from docsynth.core.pipeline.export import ExportStats, export_run
from docsynth.core.pipeline.golden import decode_shard, encode_shard
from docsynth.core.pipeline.manifest import (
    RunManifest,
    UnitManifest,
    enumerate_document_keys,
    is_complete,
    read_run_manifest,
    verify_run,
)
from docsynth.core.pipeline.pdf import PdfRenderer
from docsynth.core.pipeline.planner import plan_units
from docsynth.core.pipeline.renderer import DocumentRenderer, HtmlRenderer, RenderedDocument
from docsynth.core.pipeline.run import create_run, resume_run, work_run
from docsynth.core.pipeline.source import DocumentSource, stable_seed
from docsynth.core.pipeline.worker import GenerationWorker, WorkerStats

__all__ = [
    "DocumentRenderer",
    "DocumentSource",
    "ExportStats",
    "GenerationWorker",
    "HtmlRenderer",
    "PdfRenderer",
    "RenderedDocument",
    "RunManifest",
    "UnitManifest",
    "WorkerStats",
    "create_run",
    "decode_shard",
    "encode_shard",
    "enumerate_document_keys",
    "export_run",
    "is_complete",
    "plan_units",
    "read_run_manifest",
    "resume_run",
    "stable_seed",
    "verify_run",
    "work_run",
]
