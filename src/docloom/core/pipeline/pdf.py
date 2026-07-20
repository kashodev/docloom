"""PDF rendering via headless Chromium (Playwright).

The production :class:`~docloom.core.pipeline.renderer.DocumentRenderer`: golden
record → archetype HTML → PDF. Chromium is the right engine because the
archetypes lean on real print-layout behaviour — repeating table headers across
page breaks, `break-inside: avoid`, background colours — that a lighter
HTML-to-PDF library does not reproduce.

Three things this gets right:

* **A warm browser.** One Chromium process is launched once and reused for every
  render. Process spawn dominates per-document cost; a page is cheap, a browser
  is not. Not thread-safe — Playwright's sync objects are bound to their creating
  thread, so use one renderer per worker process.
* **"Page N of M".** Chromium's own header template fills `pageNumber` /
  `totalPages` — the only reliable way to get running page numbers, since CSS
  margin boxes are unsupported in Chromium's print path. The surrounding text is
  localised through the same label dictionary the body uses, so a French invoice
  reads "Page 2 sur 23".
* **Import-safe.** Playwright is lazy-imported and the browser is injectable, so
  this module loads without Playwright installed and its orchestration is tested
  against a fake browser; a real render is covered by a Chromium-gated test.

Not yet handled: the `(cont'd)` marker on a section that spans a page break.
Detecting where breaks landed needs post-layout measurement Chromium does not
expose simply; the repeating `<thead>` already gives an extractor the structural
continuation cue, so the text marker is deferred (the CSS hides the empty
placeholder). See TODO.md.
"""

from __future__ import annotations

import html
from typing import Any

from docloom.core.pack import DocumentPack
from docloom.core.pipeline.renderer import RenderedDocument
from docloom.core.record import GoldenRecord
from docloom.core.render import render_record

# Page geometry, in millimetres. Top/bottom margins leave room for the running
# header/footer; horizontal padding on the header aligns it with the body.
_MARGIN = {"top": "18mm", "bottom": "16mm", "left": "14mm", "right": "14mm"}
_SIDE_PAD_MM = 14


class PdfRenderer:
    """Renders a record to PDF, reusing one warm Chromium browser."""

    def __init__(
        self,
        pack: DocumentPack,
        *,
        page_size: str = "A4",
        browser: Any | None = None,
    ) -> None:
        self._pack = pack
        self._page_size = page_size
        self._browser = browser
        self._playwright: Any | None = None
        self._owns_browser = browser is None

    def _ensure_browser(self) -> Any:
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ImportError(
                    "PDF rendering needs Playwright — pip install playwright && "
                    "playwright install chromium"
                ) from exc
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        return self._browser

    def render(self, record: GoldenRecord) -> RenderedDocument:
        html_str = render_record(self._pack, record)
        browser = self._ensure_browser()
        page = browser.new_page()
        try:
            # print media so the archetypes' @print rules (table-header-group,
            # break-inside) take effect, and backgrounds so accent/zebra show.
            page.emulate_media(media="print")
            page.set_content(html_str, wait_until="load")
            pdf_bytes = page.pdf(
                format=self._page_size,
                margin=_MARGIN,
                print_background=True,
                display_header_footer=True,
                header_template=self._header_template(record),
                footer_template='<span></span>',   # empty but non-default (hides Chromium's)
            )
        finally:
            page.close()
        return RenderedDocument(
            data=pdf_bytes, content_type="application/pdf", extension=".pdf"
        )

    def _header_template(self, record: GoldenRecord) -> str:
        """The running header: issuer + invoice number left, page count right.

        The page-count phrasing comes from the pack's ``page_of`` label with the
        ``{page}`` / ``{pages}`` placeholders swapped for Chromium's counters, so
        it localises for free — "Page 2 of 23" / "Page 2 sur 23".
        """
        labels = self._pack.labels
        language = record.locale.language   # kernel-level: every record has a locale
        # Issuer and invoice number are invoice-specific; read defensively so the
        # renderer stays document-agnostic (a record without them gets a header
        # of just the page counter). When a second document type needs a
        # different header, this composition should move to the pack — see
        # TODO.md. Not abstracted for a single pack.
        issuer = html.escape(getattr(getattr(record, "issuer", None), "name", "") or "")
        number = html.escape(getattr(record, "invoice_number", "") or "")
        try:
            number_label = html.escape(labels.get(language, "invoice_number"))
            page_label = labels.get(language, "page_of")
        except KeyError:
            number_label, page_label = "", "Page {page} of {pages}"

        page_html = page_label.replace(
            "{page}", '<span class="pageNumber"></span>'
        ).replace("{pages}", '<span class="totalPages"></span>')

        left = f"{issuer} — {number_label} {number}" if issuer else f"{number_label} {number}"
        return (
            f'<div style="font-size:8px; color:#666; width:100%; '
            f'padding:0 {_SIDE_PAD_MM}mm; display:flex; justify-content:space-between;">'
            f"<span>{left}</span><span>{page_html}</span></div>"
        )

    def close(self) -> None:
        if self._owns_browser and self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> PdfRenderer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
