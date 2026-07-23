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

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from docloom.core.content import ContentCapability
from docloom.core.locale.labels import LabelRegistry
from docloom.core.record import GoldenRecord
from docloom.core.selection import Selection


@dataclass(frozen=True, slots=True)
class RunningHeader:
    """The per-page running header a pack supplies to the PDF renderer.

    The renderer is document-agnostic: it lays out ``primary`` on the left and the
    page counter on the right, and knows nothing about issuers or invoice numbers.
    The pack composes and localises both fields.

    * ``primary`` — plain text for the left side (the renderer HTML-escapes it),
      e.g. ``"Northwind Supply — Invoice INV-2026-0042"``.
    * ``page_label`` — the localised page-counter template with ``{page}`` and
      ``{pages}`` placeholders the renderer swaps for Chromium's counters, e.g.
      ``"Page {page} of {pages}"`` / ``"Page {page} sur {pages}"``.
    """

    primary: str
    page_label: str

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
    def content_capability(self) -> ContentCapability:
        """Where this pack's content comes from — see :mod:`docloom.core.content`.

        Declares whether a full dataset can be generated offline with no key
        (procedural, like invoices) or needs an LLM catalogue step (text-heavy
        types like contracts). Read it through
        :func:`~docloom.core.content.capability_of`, which defaults to procedural
        for a pack that predates this contract.
        """
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

    def header_fields(self, record: GoldenRecord) -> RunningHeader:
        """The running header for this record — see :class:`RunningHeader`.

        Owns the document-specific composition (which identifiers go in the
        header) and its localisation, so the PDF renderer needs no knowledge of
        any pack's record shape.
        """
        ...

    def default_source(
        self,
        *,
        selection: Selection | None = None,
        max_line_items: int | None = None,
        catalogue: str | None = None,
    ) -> DocumentSource:
        """The pack's default document source for a run.

        ``catalogue`` optionally names a published content artifact
        (``file://`` / ``gs://`` / ``s3://``) to draw from instead of whatever the
        pack builds in. Reading it needs no credential, so a pack with a rich
        catalogue is as local-first as one without.

        ``selection`` constrains the *population* the source draws from — which
        locales, companies, templates and capture conditions this slice of the
        run is allowed to produce. The kernel does not interpret it: it carries
        the declaration here and the pack decides what it can honour, raising
        :class:`~docloom.core.selection.UnsupportedConstraint` for what it
        cannot. Silently ignoring a constraint is not an option — a slice named
        ``french`` that emits English documents is a wasted run nobody notices.

        Whether this is key-free is a *pack* property, not a kernel guarantee.
        The invoice pack returns the sampler over its procedural seed catalogue,
        so an invoice run needs no API keys and is local-first. A text-heavy pack
        (contracts, whose clauses are generated natural language) returns an
        LLM-backed source instead — a provider and a key — behind the same run
        loop. See README § "Local-first is a property of the pack".
        """
        ...
