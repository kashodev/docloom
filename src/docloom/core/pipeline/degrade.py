"""Capture-condition post-processing — turn a clean PDF into a scanned one.

The renderer produces a crisp, digital PDF with a text layer. Real extraction
targets are rarely that: they are photocopies, phone photos, and faxes — skewed,
blurred, speckled, and with no text layer at all, so OCR is forced to actually
read the pixels. This module realises a document's :class:`DocumentCondition` by
rasterising the clean PDF and degrading each page image, then re-wrapping the
images as an image-only PDF.

Applies to any document type — a scanned contract degrades exactly like a scanned
invoice — so it lives in the kernel, keyed only off the condition and a seed.
Deterministic: the same document and condition degrade identically, so a run is
reproducible and the golden row's ``condition`` always matches the artefact.

``HANDWRITTEN`` here is degradation plus procedural ink overlays (a signature, a
stamp) — a scanned form someone wrote on. Rendering document *text* in a
handwriting font is a heavier, render-time change (a handwriting archetype +
bundled font); this covers the post-processing half. See TODO.md.
"""

from __future__ import annotations

import io
import math
from random import Random

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter

from docloom.core.enums import DocumentCondition


def rasterize(pdf_bytes: bytes, *, dpi: int = 150) -> list[Image.Image]:
    """Render each PDF page to an RGB image at ``dpi`` (self-contained, no poppler)."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        scale = dpi / 72.0
        return [doc[i].render(scale=scale).to_pil().convert("RGB") for i in range(len(doc))]
    finally:
        doc.close()


def images_to_pdf(images: list[Image.Image], *, dpi: int = 150) -> bytes:
    """Wrap page images as a single image-only PDF (no text layer, like a scan)."""
    buf = io.BytesIO()
    head, *rest = images
    head.save(buf, format="PDF", resolution=float(dpi), save_all=True, append_images=rest)
    return buf.getvalue()


# ── Per-page degradation ────────────────────────────────────────────────────
def _rotate(img: Image.Image, rng: Random, max_deg: float) -> Image.Image:
    angle = rng.uniform(-max_deg, max_deg)
    return img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))


def _add_noise(img: Image.Image, rng: Random, sigma: float) -> Image.Image:
    arr = np.asarray(img, dtype=np.float32)
    gen = np.random.default_rng(rng.getrandbits(63))
    noisy = arr + gen.normal(0.0, sigma, arr.shape).astype(np.float32)
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8), mode="RGB")


def _speckle(img: Image.Image, rng: Random, amount: float) -> Image.Image:
    """Sparse dark specks — dust and toner scatter on a photocopy."""
    arr = np.asarray(img).copy()
    gen = np.random.default_rng(rng.getrandbits(63))
    mask = gen.random(arr.shape[:2]) < amount
    arr[mask] = gen.integers(0, 80, size=(int(mask.sum()), 3), dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _jpeg_artifacts(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _ink_overlays(img: Image.Image, rng: Random) -> Image.Image:
    """Procedural handwritten ink: a signature scrawl and a slanted stamp."""
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    ink = (18, 24, 90)   # dark blue pen

    # Signature: a jittered polyline across the lower third.
    x0, y0 = int(w * 0.55), int(h * 0.80)
    pts = []
    x = x0
    for i in range(28):
        x += w * 0.012
        y = y0 + math.sin(i * 0.9) * h * 0.012 + rng.uniform(-4, 4)
        pts.append((x, y))
    draw.line(pts, fill=ink, width=2, joint="curve")

    # A slanted "PAID" stamp in translucent red, rotated onto the page.
    stamp = Image.new("RGBA", (int(w * 0.28), int(h * 0.09)), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.rectangle([2, 2, stamp.width - 3, stamp.height - 3], outline=(170, 30, 30, 210), width=3)
    sd.text((stamp.width * 0.16, stamp.height * 0.28), "PAID", fill=(170, 30, 30, 210))
    stamp = stamp.rotate(rng.uniform(-18, -8), expand=True, resample=Image.BICUBIC)
    img.paste(stamp, (int(w * 0.12), int(h * 0.15)), stamp)
    return img


#: Per-condition degradation parameters. CLEAN is absent — it is a no-op.
_PROFILES = {
    DocumentCondition.LIGHT_SCAN: {"rot": 0.6, "blur": 0.4, "noise": 6.0, "speckle": 0.0004,
                                   "jpeg": 80, "grayscale": False, "ink": False},
    DocumentCondition.HEAVY_SCAN: {"rot": 1.8, "blur": 1.0, "noise": 14.0, "speckle": 0.002,
                                   "jpeg": 45, "grayscale": True, "ink": False},
    DocumentCondition.HANDWRITTEN: {"rot": 1.4, "blur": 0.8, "noise": 12.0, "speckle": 0.0015,
                                    "jpeg": 55, "grayscale": False, "ink": True},
}


def degrade_image(img: Image.Image, condition: DocumentCondition, rng: Random) -> Image.Image:
    """Apply one condition's degradation to a single page image.

    Order matters: ink is laid down first (it is on the paper), then the whole
    page is skewed, blurred, noised, speckled and JPEG-crushed as one capture.
    """
    profile = _PROFILES.get(condition)
    if profile is None:
        return img.convert("RGB")   # CLEAN or unknown → unchanged
    out = img.convert("RGB")
    if profile["ink"]:
        out = _ink_overlays(out, rng)
    out = _rotate(out, rng, profile["rot"])
    if profile["blur"]:
        out = out.filter(ImageFilter.GaussianBlur(profile["blur"]))
    if profile["noise"]:
        out = _add_noise(out, rng, profile["noise"])
    if profile["speckle"]:
        out = _speckle(out, rng, profile["speckle"])
    if profile["grayscale"]:
        out = out.convert("L").convert("RGB")
    if profile["jpeg"]:
        out = _jpeg_artifacts(out, profile["jpeg"])
    return out


def degrade_pdf(
    pdf_bytes: bytes, condition: DocumentCondition, *, seed: int, dpi: int = 150
) -> bytes:
    """Realise ``condition`` on a clean PDF, returning a degraded image-only PDF.

    ``CLEAN`` returns the input untouched (text layer intact). Every other
    condition rasterises, degrades each page from the same seeded RNG, and
    re-wraps — so the result has no text layer, exactly like a real scan.
    """
    if condition is DocumentCondition.CLEAN:
        return pdf_bytes
    rng = Random(seed)
    pages = [degrade_image(p, condition, rng) for p in rasterize(pdf_bytes, dpi=dpi)]
    return images_to_pdf(pages, dpi=dpi)
