"""The document-pack contract.

A *pack* teaches the kernel how to produce one document type. It owns its record
model, its templates, its label vocabulary, and the mapping from record to
render context. The kernel owns everything else: locale formatting, the Jinja
environment, storage, run state, LLM providers, and export sinks.

The protocol is kept deliberately small. A pack interface designed against a
single document type will be wrong somewhere, and the cheapest way to absorb
that is to give packs wide latitude and the kernel narrow expectations. When a
second pack needs something, hoist it then — not before.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from docloom.core.locale.labels import LabelRegistry
from docloom.core.record import GoldenRecord

if TYPE_CHECKING:
    # Forward-referenced to avoid a package import cycle: core.pipeline imports
    # core.pack (the renderers), so core.pack must not import core.pipeline at
    # module load. The annotation is a string; the real type is only needed for
    # checking.
    from docloom.core.pipeline.source import DocumentSource


@runtime_checkable
class DocumentPack(Protocol):
    """Everything the kernel needs to render and export one document type."""

    #: Stable identifier used in config (``document.pack: invoice``) and as the
    #: key under which the pack registers itself.
    name: str

    @property
    def template_root(self) -> Path:
        """Directory holding this pack's archetype templates."""
        ...

    @property
    def labels(self) -> LabelRegistry:
        """Printed vocabulary for every language this pack supports."""
        ...

    @property
    def table_names(self) -> tuple[str, ...]:
        """Names of the tables :meth:`GoldenRecord.to_rows` will produce.

        Declared up front so a sink can create or validate destinations before
        the first record is generated, rather than discovering the schema
        halfway through a run.
        """
        ...

    def build_context(self, record: GoldenRecord) -> dict[str, Any]:
        """Turn a record into a Jinja context.

        Must include ``locale``, ``currency``, and ``labels`` — the kernel's
        formatting filters read those from the context so templates never pass
        them explicitly.
        """
        ...

    def archetype_for(self, record: GoldenRecord) -> str:
        """Template name (without extension) to render this record with."""
        ...

    def default_source(self) -> DocumentSource:
        """The pack's default document source for a run.

        Whether this is key-free is a *pack* property, not a kernel guarantee.
        The invoice pack returns the sampler over its procedural seed catalogue,
        so an invoice run needs no API keys and is local-first. A text-heavy pack
        (contracts, whose clauses are generated natural language) returns an
        LLM-backed source instead — a provider and a key — behind the same run
        loop. See README § "Local-first is a property of the pack".
        """
        ...
