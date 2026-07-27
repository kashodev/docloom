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

This module deliberately adds **no marks of its own**. Handwriting, signatures and
stamps belong to the *renderer* — the invoice pack's hand-filled pad archetype
draws them in real handwriting faces with roughened ink edges — so by the time a
page reaches here the ink is already on the paper and degrades along with it,
which is how a scanned handwritten form actually looks. Drawing them here instead
was tried and it showed: a procedurally drawn signature and stamp read as vector
art, not ink.
"""

from __future__ import annotations

import io
from random import Random
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter

from docsynth.core.enums import DocumentCondition


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


#: Per-condition degradation parameters. CLEAN is absent — it is a no-op.
#:
#: ``HANDWRITTEN`` degrades like a light-to-mid scan and adds no marks of its
#: own: the handwriting, signature and stamp are drawn by the *renderer* (the
#: hand-filled pad archetype), so they are already on the paper by the time this
#: runs and degrade along with it — which is exactly how real ink behaves. An
#: earlier version drew them here with PIL and it showed: perfectly periodic
#: signature strokes and razor-sharp stamp borders read as vector art, not ink.
_PROFILES = {
    DocumentCondition.LIGHT_SCAN: {"rot": 0.6, "blur": 0.4, "noise": 6.0, "speckle": 0.0004,
                                   "jpeg": 80, "grayscale": False},
    DocumentCondition.HEAVY_SCAN: {"rot": 1.8, "blur": 1.0, "noise": 14.0, "speckle": 0.002,
                                   "jpeg": 45, "grayscale": True},
    DocumentCondition.HANDWRITTEN: {"rot": 1.4, "blur": 0.8, "noise": 11.0, "speckle": 0.0012,
                                    "jpeg": 58, "grayscale": False},
}


#: JPEG quality a pristine capture is saved at. A crisp document is still a
#: *photocopy or scan*, not a lossless original, so this is high but not 100.
_CRISP_JPEG = 94
#: Floor on the degradation multiplier at ``wear`` 0 — a well-kept document still
#: went through a scanner, so it keeps a trace of skew and grain.
_MIN_WEAR_FACTOR = 0.15


def scale_profile(profile: dict[str, Any], wear: float) -> dict[str, Any]:
    """A degradation profile scaled by how worn the artefact is.

    ``wear`` 1.0 leaves the profile alone (the well-used default). Lower values
    ease off the skew, blur, noise and speckle together and raise the JPEG
    quality toward :data:`_CRISP_JPEG`, so a crisp document is sharp without ever
    becoming a pristine digital render — it is still a captured piece of paper.

    Pure, so the dial's behaviour is unit-tested without rasterising anything.
    """
    wear = min(max(wear, 0.0), 1.0)
    factor = _MIN_WEAR_FACTOR + (1.0 - _MIN_WEAR_FACTOR) * wear
    scaled = dict(profile)
    for key in ("rot", "blur", "noise", "speckle"):
        scaled[key] = profile[key] * factor
    base_jpeg = profile["jpeg"]
    scaled["jpeg"] = round(base_jpeg + (_CRISP_JPEG - base_jpeg) * (1.0 - wear))
    return scaled


def degrade_image(
    img: Image.Image, condition: DocumentCondition, rng: Random, *, wear: float = 1.0
) -> Image.Image:
    """Apply one condition's degradation to a single page image.

    One capture, in order: skew, then blur, noise, speckle and a JPEG crush —
    everything already on the page (including handwritten ink) degrades together.
    """
    profile = _PROFILES.get(condition)
    if profile is None:
        return img.convert("RGB")   # CLEAN or unknown → unchanged
    profile = scale_profile(profile, wear)
    out = img.convert("RGB")
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
    pdf_bytes: bytes,
    condition: DocumentCondition,
    *,
    seed: int,
    dpi: int = 150,
    wear: float = 1.0,
) -> bytes:
    """Realise ``condition`` on a clean PDF, returning a degraded image-only PDF.

    ``CLEAN`` returns the input untouched (text layer intact). Every other
    condition rasterises, degrades each page from the same seeded RNG, and
    re-wraps — so the result has no text layer, exactly like a real scan.

    ``wear`` (0..1) scales how battered the result looks; pass the record's own
    value so the post-process agrees with the ink roughening already rendered
    into the page. See :func:`scale_profile`.
    """
    if condition is DocumentCondition.CLEAN:
        return pdf_bytes
    rng = Random(seed)
    pages = [degrade_image(p, condition, rng, wear=wear)
             for p in rasterize(pdf_bytes, dpi=dpi)]
    return images_to_pdf(pages, dpi=dpi)
