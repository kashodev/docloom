"""Capture-condition degradation tests.

The per-page image degradation is pure and deterministic, tested on a synthetic
page so effects are measurable without Chromium. The PDF round-trip
(rasterise -> degrade -> re-wrap) is exercised on a small image PDF built in the
test, so it needs no renderer either.
"""

from __future__ import annotations

import io
from random import Random

import numpy as np
import pytest
from PIL import Image, ImageDraw

from docloom.core.enums import DocumentCondition
from docloom.core.pipeline.degrade import (
    degrade_image,
    degrade_pdf,
    images_to_pdf,
    rasterize,
)


def a_page(w: int = 300, h: int = 400) -> Image.Image:
    """A light page with dark content, so degradation is visible (pure white
    would just clip noise back to white)."""
    img = Image.new("RGB", (w, h), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([30, 30, w - 30, 80], fill=(20, 20, 20))
    for y in range(120, h - 40, 24):
        d.line([(30, y), (w - 30, y)], fill=(40, 40, 40), width=3)
    return img


def _arr(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"), dtype=np.int16)


# ── Per-page effects ────────────────────────────────────────────────────────
def test_clean_is_a_no_op() -> None:
    page = a_page()
    out = degrade_image(page, DocumentCondition.CLEAN, Random(1))
    assert np.array_equal(_arr(out), _arr(page))


@pytest.mark.parametrize("condition", [
    DocumentCondition.LIGHT_SCAN,
    DocumentCondition.HEAVY_SCAN,
    DocumentCondition.HANDWRITTEN,
])
def test_degradation_changes_pixels_but_keeps_the_page_size(condition: DocumentCondition) -> None:
    page = a_page()
    out = degrade_image(page, condition, Random(7))
    assert out.size == page.size
    # Meaningfully different from the clean page.
    assert np.abs(_arr(out) - _arr(page)).mean() > 1.0


def test_degradation_is_deterministic_per_seed() -> None:
    page = a_page()
    one = degrade_image(page, DocumentCondition.HEAVY_SCAN, Random(42))
    two = degrade_image(page, DocumentCondition.HEAVY_SCAN, Random(42))
    assert np.array_equal(_arr(one), _arr(two))
    # A different seed gives a different degradation (skew/noise differ).
    three = degrade_image(page, DocumentCondition.HEAVY_SCAN, Random(43))
    assert not np.array_equal(_arr(one), _arr(three))


def test_heavy_scan_is_desaturated() -> None:
    out = degrade_image(a_page(), DocumentCondition.HEAVY_SCAN, Random(1))
    arr = np.asarray(out).astype(np.int16)
    # Grayscale-through-RGB: channels are (near) equal per pixel.
    assert np.abs(arr[..., 0] - arr[..., 1]).mean() < 3
    assert np.abs(arr[..., 1] - arr[..., 2]).mean() < 3


def test_handwritten_lays_down_coloured_ink() -> None:
    page = a_page()
    plain = degrade_image(page, DocumentCondition.HEAVY_SCAN, Random(3))
    inked = degrade_image(page, DocumentCondition.HANDWRITTEN, Random(3))
    # The stamp/signature introduce saturated colour the scan paths do not.
    def max_channel_spread(img: Image.Image) -> int:
        a = np.asarray(img).astype(np.int16)
        return int((a.max(axis=2) - a.min(axis=2)).max())
    assert max_channel_spread(inked) > max_channel_spread(plain)


# ── PDF round-trip ──────────────────────────────────────────────────────────
def _clean_pdf(pages: int = 2) -> bytes:
    return images_to_pdf([a_page() for _ in range(pages)])


def test_rasterize_recovers_every_page() -> None:
    imgs = rasterize(_clean_pdf(pages=3), dpi=100)
    assert len(imgs) == 3
    assert all(im.mode == "RGB" for im in imgs)


def test_degrade_pdf_clean_returns_input_untouched() -> None:
    clean = _clean_pdf()
    assert degrade_pdf(clean, DocumentCondition.CLEAN, seed=1) is clean


def test_degrade_pdf_produces_a_valid_multipage_image_pdf() -> None:
    clean = _clean_pdf(pages=2)
    out = degrade_pdf(clean, DocumentCondition.LIGHT_SCAN, seed=5, dpi=100)
    assert out.startswith(b"%PDF")
    # Same page count, and it rasterises back to two pages.
    assert len(rasterize(out, dpi=72)) == 2
    # The degraded pages differ from the clean rasterisation. (Re-rasterising
    # image PDFs can round the page height by a pixel, so crop to the overlap.)
    clean_px = _arr(rasterize(clean, dpi=100)[0])
    deg_px = _arr(rasterize(out, dpi=100)[0])
    h = min(clean_px.shape[0], deg_px.shape[0])
    w = min(clean_px.shape[1], deg_px.shape[1])
    assert np.abs(clean_px[:h, :w] - deg_px[:h, :w]).mean() > 1.0


def test_degrade_pdf_is_deterministic_at_the_pixel_level() -> None:
    clean = _clean_pdf()
    a = rasterize(degrade_pdf(clean, DocumentCondition.HEAVY_SCAN, seed=9, dpi=100), dpi=100)[0]
    b = rasterize(degrade_pdf(clean, DocumentCondition.HEAVY_SCAN, seed=9, dpi=100), dpi=100)[0]
    assert np.array_equal(_arr(a), _arr(b))
