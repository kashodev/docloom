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
import math
from typing import Any

from docloom.core.pack import DocumentPack
from docloom.core.pipeline.renderer import RenderedDocument
from docloom.core.record import GoldenRecord
from docloom.core.render import render_record

# Page geometry, in millimetres. Top/bottom margins leave room for the running
# header/footer; horizontal padding on the header aligns it with the body.
_MARGIN = {"top": "18mm", "bottom": "16mm", "left": "14mm", "right": "14mm"}
_SIDE_PAD_MM = 14
_MARGIN_V_MM = 18 + 16   # total vertical margin removed from the printable area
_MARGIN_H_MM = 14 + 14   # total horizontal margin removed from the printable area
_PX_PER_MM = 96 / 25.4   # CSS pixels per millimetre at 96 dpi
_PAGE_HEIGHT_MM = {"A4": 297.0, "Letter": 279.4}
_PAGE_WIDTH_MM = {"A4": 210.0, "Letter": 215.9}
_MIN_LAST_PAGE_FILL = 0.25   # a trailing page must be at least this full
_MIN_FIT_SCALE = 0.80        # never shrink content below this to avoid a sliver


def fit_scale(
    content_px: float,
    printable_px: float,
    *,
    min_fill: float = _MIN_LAST_PAGE_FILL,
    min_scale: float = _MIN_FIT_SCALE,
) -> float:
    """The PDF scale that avoids a near-empty trailing page.

    If content spills onto a last page that is less than ``min_fill`` full, shrink
    it just enough to land on one fewer page — but never below ``min_scale`` (a
    hard-to-read shrink is worse than an extra page). Single-page and
    already-well-filled documents render at 1.0.
    """
    if content_px <= printable_px or printable_px <= 0:
        return 1.0
    pages = math.ceil(content_px / printable_px)
    last_fill = (content_px - (pages - 1) * printable_px) / printable_px
    if last_fill >= min_fill:
        return 1.0
    scale = (pages - 1) * printable_px / content_px
    return max(round(scale, 4), min_scale)


class PdfRenderer:
    """Renders a record to PDF, reusing one warm Chromium browser."""

    def __init__(
        self,
        pack: DocumentPack,
        *,
        page_size: str = "A4",
        browser: Any | None = None,
        fit_pages: bool = True,
    ) -> None:
        self._pack = pack
        self._page_size = page_size
        self._browser = browser
        self._fit_pages = fit_pages
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
        page = self._prepared_page(render_record(self._pack, record))
        try:
            pdf_bytes = page.pdf(
                format=self._page_size,
                margin=_MARGIN,
                scale=self._fit_scale(page),
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

    def _prepared_page(self, html_str: str) -> Any:
        """A page laid out exactly as it will print, ready to measure or emit.

        The viewport is pinned to the *printable* width — page width minus the
        left/right margins — so ``scrollHeight`` reflects how the content wraps
        and stacks under print pagination. Measuring at the browser's default
        (much wider) viewport gives a height that has nothing to do with the
        printed page and makes the fit pass wildly wrong.
        """
        page = self._ensure_browser().new_page()
        w, h = self._printable_size()
        page.set_viewport_size({"width": max(int(round(w)), 1), "height": max(int(round(h)), 1)})
        # print media so the archetypes' @print rules (table-header-group,
        # break-inside) take effect, and backgrounds so accent/zebra show.
        page.emulate_media(media="print")
        page.set_content(html_str, wait_until="load")
        return page

    def _printable_size(self) -> tuple[float, float]:
        page_h = _PAGE_HEIGHT_MM.get(self._page_size, _PAGE_HEIGHT_MM["A4"])
        page_w = _PAGE_WIDTH_MM.get(self._page_size, _PAGE_WIDTH_MM["A4"])
        return (page_w - _MARGIN_H_MM) * _PX_PER_MM, (page_h - _MARGIN_V_MM) * _PX_PER_MM

    def _printable_px(self) -> float:
        return self._printable_size()[1]

    def _fit_scale(self, page: Any) -> float:
        """Measure the laid-out content and choose a scale that avoids a
        near-empty trailing page (see :func:`fit_scale`)."""
        if not self._fit_pages:
            return 1.0
        content_px = float(page.evaluate("document.documentElement.scrollHeight"))
        return fit_scale(content_px, self._printable_px())

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
