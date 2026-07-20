"""PDF renderer tests.

Two layers. The **orchestration** — that the renderer builds the right header,
passes the rendered HTML to Chromium, reuses one browser, and returns a PDF
``RenderedDocument`` — is tested against a fake browser, so it runs with no
Chromium. The **real render** — that Chromium actually produces a valid,
correctly-paginated PDF — is gated on Playwright + a browser being installed,
and skipped otherwise (like the Firestore emulator test).
"""

from __future__ import annotations

import io

import pytest

import docloom.packs  # noqa: F401  — registers the invoice pack
from docloom.core import Currency, Jurisdiction, Locale, get_pack
from docloom.core.pipeline.pdf import PdfRenderer
from tests.factories import invoice, simple_lines, telecom_lines


# ── Fake Chromium — records what the renderer asked it to do ────────────────
class FakePage:
    def __init__(self, log: dict) -> None:
        self._log = log

    def emulate_media(self, media: str) -> None:
        self._log["media"] = media

    def set_content(self, html: str, wait_until: str = "load") -> None:
        self._log["html"] = html

    def pdf(self, **kwargs) -> bytes:  # noqa: ANN003
        self._log["pdf_kwargs"] = kwargs
        return b"%PDF-1.7\nfake"

    def close(self) -> None:
        self._log["closed"] = self._log.get("closed", 0) + 1


class FakeBrowser:
    def __init__(self) -> None:
        self.log: dict = {}
        self.pages_opened = 0

    def new_page(self) -> FakePage:
        self.pages_opened += 1
        return FakePage(self.log)


# ── Orchestration (no Chromium) ─────────────────────────────────────────────

def test_render_returns_pdf_document() -> None:
    r = PdfRenderer(get_pack("invoice"), browser=FakeBrowser())
    doc = r.render(invoice(simple_lines()))
    assert doc.content_type == "application/pdf"
    assert doc.extension == ".pdf"
    assert doc.data.startswith(b"%PDF")


def test_render_passes_the_rendered_html_to_the_browser() -> None:
    browser = FakeBrowser()
    r = PdfRenderer(get_pack("invoice"), browser=browser)
    r.render(invoice(simple_lines()))
    assert "<!doctype html>" in browser.log["html"]
    assert "Northwind Supply" in browser.log["html"]
    assert browser.log["media"] == "print"          # print CSS applies


def test_pdf_call_enables_running_header_and_backgrounds() -> None:
    browser = FakeBrowser()
    PdfRenderer(get_pack("invoice"), browser=browser).render(invoice(simple_lines()))
    kw = browser.log["pdf_kwargs"]
    assert kw["display_header_footer"] is True
    assert kw["print_background"] is True
    assert kw["format"] == "A4"
    assert "pageNumber" in kw["header_template"]
    assert "totalPages" in kw["header_template"]
    assert "INV-2026-0042" in kw["header_template"]   # invoice number in the header


def test_header_is_localised_via_the_label_dictionary() -> None:
    browser = FakeBrowser()
    fr = invoice(simple_lines(), locale=Locale.FR_FR,
                 jurisdiction=Jurisdiction.FR, currency=Currency.EUR)
    PdfRenderer(get_pack("invoice"), browser=browser).render(fr)
    header = browser.log["pdf_kwargs"]["header_template"]
    assert "Page" in header and "sur" in header       # fr-FR: "Page … sur …"
    assert "Facture n°" in header                      # fr-FR invoice-number label


def test_browser_is_reused_across_renders() -> None:
    browser = FakeBrowser()
    r = PdfRenderer(get_pack("invoice"), browser=browser)
    r.render(invoice(simple_lines()))
    r.render(invoice(simple_lines()))
    assert browser.pages_opened == 2                   # two pages, one browser
    assert browser.log["closed"] == 2                  # each page closed


def test_page_size_is_configurable() -> None:
    browser = FakeBrowser()
    PdfRenderer(get_pack("invoice"), page_size="Letter", browser=browser).render(
        invoice(simple_lines())
    )
    assert browser.log["pdf_kwargs"]["format"] == "Letter"


def test_injected_browser_is_not_closed_by_close() -> None:
    """The renderer owns only a browser it launched itself."""
    browser = FakeBrowser()
    r = PdfRenderer(get_pack("invoice"), browser=browser)
    r.close()                                          # must not touch an injected browser
    r.render(invoice(simple_lines()))                  # still usable


# ── Real Chromium render (gated) ────────────────────────────────────────────
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="needs Playwright + an installed Chromium"
)


@requires_chromium
def test_real_render_produces_a_valid_pdf() -> None:
    import pikepdf

    with PdfRenderer(get_pack("invoice")) as r:
        doc = r.render(invoice(simple_lines()))
    assert doc.data.startswith(b"%PDF-")
    assert len(pikepdf.open(io.BytesIO(doc.data)).pages) == 1


@requires_chromium
def test_real_render_paginates_a_long_document() -> None:
    """The telecom archetype with many rows must break across pages — proving
    the repeating-header pagination actually works in Chromium, not just in CSS."""
    import pikepdf

    from docloom.packs.invoice import RenderProfile

    long_bill = invoice(telecom_lines() * 40)
    long_bill = long_bill.model_copy(update={
        "render_profile": long_bill.render_profile.model_copy(
            update={"archetype": "telecom-itemized-37"}
        )
    })
    with PdfRenderer(get_pack("invoice")) as r:
        doc = r.render(long_bill)
    assert len(pikepdf.open(io.BytesIO(doc.data)).pages) > 1
