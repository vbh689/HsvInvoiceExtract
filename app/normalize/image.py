"""Ingest & normalize stage: accepts common image formats and PDF, corrects
orientation, downscales, and re-encodes to a single canonical format before
anything is hashed or sent to a model. This step affects extraction accuracy
more than model choice — every page a model ever sees has gone through it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

import pypdfium2 as pdfium
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - optional HEIC support
    pass

PDF_MAGIC = b"%PDF-"
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBP_MAGIC = b"RIFF"  # confirmed at offset 8

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
SUPPORTED_PDF_TYPES = {"application/pdf"}
SUPPORTED_CONTENT_TYPES = SUPPORTED_IMAGE_TYPES | SUPPORTED_PDF_TYPES


def detect_content_type(raw_bytes: bytes) -> str | None:
    """Detect content type from magic bytes when the declared type is
    application/octet-stream or otherwise unreliable."""
    if raw_bytes[:5] == PDF_MAGIC:
        return "application/pdf"
    if raw_bytes[:3] == JPEG_MAGIC:
        return "image/jpeg"
    if raw_bytes[:8] == PNG_MAGIC:
        return "image/png"
    if raw_bytes[:4] == WEBP_MAGIC and raw_bytes[8:12] == b"WEBP":
        return "image/webp"
    if raw_bytes[4:8] in (b"ftyp", b"ftyp"):
        # HEIC / HEIF (ISOBMFF container)
        ftyp = raw_bytes[8:12]
        if ftyp in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
        return "image/heif"
    return None


@dataclass(frozen=True)
class NormalizedDocument:
    pages: list[bytes]  # re-encoded JPEG bytes, one per page, orientation-corrected
    content_hash: str  # sha256 over all pages, order-preserving


def is_pdf(content_type: str | None, raw_bytes: bytes) -> bool:
    if content_type and "pdf" in content_type.lower():
        return True
    return raw_bytes[:5] == PDF_MAGIC


def _load_pdf_pages(raw_bytes: bytes, dpi: int) -> list[Image.Image]:
    pdf = pdfium.PdfDocument(raw_bytes)
    scale = dpi / 72
    return [page.render(scale=scale).to_pil().convert("RGB") for page in pdf]


def _load_image_pages(raw_bytes: bytes) -> list[Image.Image]:
    img = Image.open(BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)  # apply + strip orientation tag, before anything else
    return [img.convert("RGB")]


def load_pages(
    raw_bytes: bytes, content_type: str | None, *, pdf_render_dpi: int
) -> list[Image.Image]:
    if is_pdf(content_type, raw_bytes):
        return _load_pdf_pages(raw_bytes, pdf_render_dpi)
    return _load_image_pages(raw_bytes)


def _encode_page(img: Image.Image, *, max_dimension: int, jpeg_quality: int) -> bytes:
    if max(img.size) > max_dimension:
        img = img.copy()
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    return buf.getvalue()


def compute_pages_hash(pages: list[bytes]) -> str:
    h = hashlib.sha256()
    for page in pages:
        h.update(len(page).to_bytes(8, "big"))
        h.update(page)
    return h.hexdigest()


def normalize_document(
    raw_bytes: bytes,
    content_type: str | None,
    *,
    max_dimension: int,
    jpeg_quality: int,
    pdf_render_dpi: int,
) -> NormalizedDocument:
    pil_pages = load_pages(raw_bytes, content_type, pdf_render_dpi=pdf_render_dpi)
    pages = [
        _encode_page(p, max_dimension=max_dimension, jpeg_quality=jpeg_quality) for p in pil_pages
    ]
    return NormalizedDocument(pages=pages, content_hash=compute_pages_hash(pages))
