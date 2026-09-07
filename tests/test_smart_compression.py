from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from pdf_optimizer.smart_images import analyze_document, optimize_raster_images

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


def test_images_in_inherited_page_resources_are_optimized(tmp_path: Path) -> None:
    source = tmp_path / "inherited.pdf"
    _write_scan_pdf(source)
    inherited = tmp_path / "inherited-resources.pdf"
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0].obj
        page["/Parent"]["/Resources"] = page["/Resources"]
        del page["/Resources"]
        pdf.save(inherited)
    result = optimize_pdf(inherited, options=OptimizationOptions(compression_level="medium"))
    assert result.images_optimized == 1


@pytest.mark.parametrize("special", ["calrgb", "embedded_mask", "oversized"])
def test_risky_images_are_left_untouched(tmp_path: Path, special: str) -> None:
    source = tmp_path / "special.pdf"
    _write_scan_pdf(source)
    with pikepdf.open(source) as pdf:
        stream = pdf.pages[0].obj["/Resources"]["/XObject"]["/Scan"]
        if special == "calrgb":
            stream["/ColorSpace"] = pikepdf.Array([
                pikepdf.Name.CalRGB,
                pikepdf.Dictionary(WhitePoint=pikepdf.Array([0.9505, 1, 1.089])),
            ])
        elif special == "embedded_mask":
            stream["/SMaskInData"] = 1
        else:
            stream["/Width"] = 1_000_000
        original = stream.read_raw_bytes()
        stats = optimize_raster_images(pdf, analyze_document(pdf), "strong")
        assert stats.images_reencoded == 0
        assert stream.read_raw_bytes() == original


def test_unsupported_decoder_does_not_abort_optimization(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "unsupported.pdf"
    _write_scan_pdf(source)

    def unsupported(_self):
        raise pikepdf.UnsupportedImageTypeError("unsupported codec")

    monkeypatch.setattr(pikepdf.PdfImage, "as_pil_image", unsupported)
    result = optimize_pdf(source, options=OptimizationOptions(compression_level="strong"))
    assert result.output_path.is_file()
    assert result.images_optimized == 0


def test_cyclic_form_resources_do_not_recurse_forever() -> None:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page()
        form = pdf.make_stream(b"")
        form["/Subtype"] = pikepdf.Name.Form
        form["/Resources"] = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Self=form))
        page.obj["/Resources"] = form["/Resources"]
        assert analyze_document(pdf).image_count == 0
