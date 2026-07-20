"""Generation pipeline — the spine that turns tested components into a run.

    plan → create → [claim → generate → render → persist → shard → complete]* → done

Document-agnostic: the worker knows the loop; a pack supplies the
:class:`DocumentSource` (how to invent a record) and the render kernel supplies
the :class:`DocumentRenderer` (record → bytes). Storage, state, and the golden
codec are the kernel's.
"""

from docloom.core.pipeline.golden import decode_shard, encode_shard
from docloom.core.pipeline.pdf import PdfRenderer
from docloom.core.pipeline.planner import plan_units
from docloom.core.pipeline.renderer import DocumentRenderer, HtmlRenderer, RenderedDocument
from docloom.core.pipeline.run import create_run, resume_run, work_run
from docloom.core.pipeline.source import DocumentSource
from docloom.core.pipeline.worker import GenerationWorker, WorkerStats

__all__ = [
    "DocumentRenderer",
    "DocumentSource",
    "GenerationWorker",
    "HtmlRenderer",
    "PdfRenderer",
    "RenderedDocument",
    "WorkerStats",
    "create_run",
    "decode_shard",
    "encode_shard",
    "plan_units",
    "resume_run",
    "work_run",
]
