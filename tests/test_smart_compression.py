from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image

from pdf_optimizer import (
    CompressionLevel,
    DocumentType,
    OptimizationOptions,
    OptimizationStatus,
    optimize_pdf,
)


def _write_scan_pdf(path: Path) -> int:
    # Deterministic photographic texture: the high-quality source JPEG leaves
    # useful headroom for the Medium preset without requiring test assets.
    image = Image.effect_noise((1200, 1600), 72).convert("RGB")
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=99, subsampling=0)
    jpeg = encoded.getvalue()
    image.close()

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(600, 800))
        image_stream = pdf.make_stream(jpeg)
        image_stream["/Type"] = pikepdf.Name.XObject
        image_stream["/Subtype"] = pikepdf.Name.Image
        image_stream["/Width"] = 1200
        image_stream["/Height"] = 1600
        image_stream["/ColorSpace"] = pikepdf.Name.DeviceRGB
        image_stream["/BitsPerComponent"] = 8
        image_stream["/Filter"] = pikepdf.Name.DCTDecode
        page.obj["/Resources"] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary({"/Scan": image_stream})
        )
        page.obj["/Contents"] = pdf.make_stream(
            b"q 600 0 0 800 0 0 cm /Scan Do Q\n"
        )
        pdf.save(path)
    return len(jpeg)


def test_medium_auto_detects_and_compresses_image_heavy_pdf(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    original_jpeg_size = _write_scan_pdf(source)

    result = optimize_pdf(
        source,
        options=OptimizationOptions(compression_level=CompressionLevel.MEDIUM),
    )

    assert result.status is OptimizationStatus.OPTIMIZED
    assert result.document_type is DocumentType.IMAGE_HEAVY
    assert result.compression_level is CompressionLevel.MEDIUM
    assert result.images_optimized == 1
    assert result.output_size < result.input_size
    with pikepdf.Pdf.open(result.output_path) as output:
        optimized_image = output.pages[0].obj["/Resources"]["/XObject"]["/Scan"]
        assert len(optimized_image.read_raw_bytes()) < original_jpeg_size
        assert int(optimized_image["/Width"]) == 1200
        assert int(optimized_image["/Height"]) == 1600


def test_strong_keeps_text_vector_pdf_on_lossless_path(tmp_path: Path) -> None:
    source = tmp_path / "vector.pdf"
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page()
        page.obj["/Contents"] = pdf.make_stream(b"0 0 m 100 100 l S\n" * 1000)
        pdf.save(source, compress_streams=False)

    result = optimize_pdf(
        source,
        options=OptimizationOptions(compression_level=CompressionLevel.STRONG),
    )

    assert result.document_type is DocumentType.TEXT_VECTOR
    assert result.images_optimized == 0
    assert result.compression_level is CompressionLevel.STRONG


def test_strong_image_preset_is_smaller_than_medium(tmp_path: Path) -> None:
    source = tmp_path / "photo-scan.pdf"
    _write_scan_pdf(source)

    medium = optimize_pdf(
        source,
        options=OptimizationOptions(compression_level=CompressionLevel.MEDIUM),
    )
    strong = optimize_pdf(
        source,
        options=OptimizationOptions(compression_level=CompressionLevel.STRONG),
    )

    assert medium.images_optimized == 1
    assert strong.images_optimized == 1
    assert strong.output_size < medium.output_size
