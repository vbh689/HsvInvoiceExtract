from io import BytesIO

import pytest
from PIL import Image

from app.normalize.image import is_pdf, normalize_document

MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj
xref
0 4
0000000000 65535 f
trailer<</Size 4/Root 1 0 R>>
startxref
0
%%EOF"""


def test_exif_orientation_correction():
    img = Image.new("RGB", (20, 10), color=(255, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90 CW to display correctly
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())

    normalized = normalize_document(
        buf.getvalue(), "image/jpeg", max_dimension=2048, jpeg_quality=90, pdf_render_dpi=200
    )
    assert len(normalized.pages) == 1
    out = Image.open(BytesIO(normalized.pages[0]))
    assert out.size == (10, 20)  # width/height swapped by the correction


def test_downscale_large_image():
    img = Image.new("RGB", (3000, 1000), color=(0, 255, 0))
    buf = BytesIO()
    img.save(buf, format="JPEG")

    normalized = normalize_document(
        buf.getvalue(), "image/jpeg", max_dimension=1000, jpeg_quality=85, pdf_render_dpi=200
    )
    out = Image.open(BytesIO(normalized.pages[0]))
    assert max(out.size) <= 1000


def test_pdf_first_page_rendered():
    assert is_pdf("application/pdf", MINIMAL_PDF)
    normalized = normalize_document(
        MINIMAL_PDF, "application/pdf", max_dimension=2048, jpeg_quality=85, pdf_render_dpi=200
    )
    assert len(normalized.pages) == 1
    out = Image.open(BytesIO(normalized.pages[0]))
    assert out.size[0] > 0 and out.size[1] > 0


def test_content_hash_is_deterministic_and_order_preserving():
    img = Image.new("RGB", (10, 10), color=(1, 2, 3))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    raw = buf.getvalue()

    a = normalize_document(
        raw, "image/jpeg", max_dimension=2048, jpeg_quality=85, pdf_render_dpi=200
    )
    b = normalize_document(
        raw, "image/jpeg", max_dimension=2048, jpeg_quality=85, pdf_render_dpi=200
    )
    assert a.content_hash == b.content_hash


def test_heic_input_decodes():
    pytest.importorskip("pillow_heif")
    img = Image.new("RGB", (20, 10), color=(10, 20, 30))
    buf = BytesIO()
    try:
        img.save(buf, format="HEIF")
    except Exception:
        pytest.skip("HEIF encoder not available in this environment")

    normalized = normalize_document(
        buf.getvalue(), "image/heic", max_dimension=2048, jpeg_quality=85, pdf_render_dpi=200
    )
    assert len(normalized.pages) == 1
    out = Image.open(BytesIO(normalized.pages[0]))
    assert out.size == (20, 10)
