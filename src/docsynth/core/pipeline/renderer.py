"""Document renderer — a record to bytes.

The worker persists whatever bytes a renderer returns, tagged with a content
type and file extension, so the same loop writes HTML today and PDF once the
Playwright renderer lands. Keeping this a protocol is what lets the PDF renderer
(and its scan-degradation variants) drop in without touching the worker.

:class:`HtmlRenderer` is the renderer available now — golden record → archetype
HTML via the render kernel. It needs no browser, so the whole pipeline is
testable end to end before Playwright exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from docsynth.core.pack import DocumentPack
from docsynth.core.record import GoldenRecord
from docsynth.core.render import render_record


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    """The bytes of one rendered document, and how to store them."""

    data: bytes
    content_type: str
    extension: str      # includes the dot, e.g. ".html", ".pdf"


@runtime_checkable
class DocumentRenderer(Protocol):
    def render(self, record: GoldenRecord) -> RenderedDocument:
        ...


class HtmlRenderer:
    """Renders a record to archetype HTML. No browser required."""

    def __init__(self, pack: DocumentPack) -> None:
        self._pack = pack

    def render(self, record: GoldenRecord) -> RenderedDocument:
        html = render_record(self._pack, record)
        return RenderedDocument(
            data=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            extension=".html",
        )
