from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdf_optimizer import CompressionLevel, OptimizationOptions, optimize_pdf
from pdf_optimizer.content_streams import _rewrite_vector_content, optimize_content_streams


def test_zero_trim_keeps_every_value_exact() -> None:
    data = b"0.480000 -0.000000 1.000000 52.799999 m\n1.500000 2.250000 l\n"
    assert _rewrite_vector_content(data, None) == b"0.48 -0 1 52.799999 m\n1.5 2.25 l\n"


def test_truncation_only_touches_path_coordinates() -> None:
    data = (
        b"0.750000 0.000000 0.000000 -0.750000 0.123456 841.920044 cm\n"
        b"0.000123 w\n"
        b"1.234567 2.345678 m\n"
        b"3.456789 4.567891 5.678912 6.789123 7.891234 8.912345 c\n"
    )
    result = _rewrite_vector_content(data, 2)
    assert result == (
        b"0.75 0 0 -0.75 0.12 841.92 cm\n"
        b"0.000123 w\n"
        b"1.23 2.34 m\n"
        b"3.45 4.56 5.67 6.78 7.89 8.91 c\n"
    )


def test_truncation_is_skipped_when_a_transform_magnifies() -> None:
    data = b"100 0 0 100 0 0 cm\n0.123456 0.654321 m\n"
    assert _rewrite_vector_content(data, 2) == b"100 0 0 100 0 0 cm\n0.123456 0.654321 m\n"


@pytest.mark.parametrize(
    "data",
    [
        b"BT /F1 12 Tf (1.500000 2.000000 m) Tj ET",
        b"q BI /W 1 /H 1 ID \x00 EI Q",
        b"/GS1 gs <</A 1.000000>> BDC",
        b"% 1.000000 comment\n1.000000 0 m",
    ],
)
def test_content_with_text_or_inline_data_is_left_alone(data: bytes) -> None:
    assert _rewrite_vector_content(data, 2) is None


def _write_outline_pdf(path: Path, pieces: int = 40) -> None:
    body = b"".join(
        b"q\n1.000000 0.000000 0.000000 1.000000 %d.123456 10.987654 cm\n"
        b"0.000000 0.000000 m\n1.280000 -1.280000 l\n-6.560000 -1.280000 l\nh\nf\nQ\n" % i
        for i in range(pieces * 30)
    )
    chunk = len(body) // pieces
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(600, 800))
        # Split on line boundaries like real producers do.
        lines = body.split(b"\n")
        step = len(lines) // pieces
        page.obj["/Contents"] = pikepdf.Array(
            [
                pdf.make_stream(b"\n".join(lines[i : i + step]) + b"\n")
                for i in range(0, len(lines), step)
            ]
        )
        pdf.save(path, compress_streams=True)
    assert chunk > 0


@pytest.mark.parametrize("level", list(CompressionLevel))
def test_outline_pdf_shrinks_and_keeps_its_geometry(tmp_path: Path, level) -> None:
    source = tmp_path / "outlines.pdf"
    _write_outline_pdf(source)

    result = optimize_pdf(
        source, tmp_path / "out", OptimizationOptions(compression_level=level)
    )

    assert result.saved_percent > 20
    with pikepdf.Pdf.open(source) as original, pikepdf.Pdf.open(result.output_path) as optimized:
        assert len(optimized.pages) == len(original.pages) == 1
        before = pikepdf.parse_content_stream(original.pages[0])
        after = pikepdf.parse_content_stream(optimized.pages[0])
        assert [str(op.operator) for op in before] == [str(op.operator) for op in after]
        tolerance = {
            CompressionLevel.MINIMUM: 0.0,
            CompressionLevel.MEDIUM: 0.001,
            CompressionLevel.STRONG: 0.01,
        }[level]
        for old, new in zip(before, after):
            for first, second in zip(old.operands, new.operands):
                assert abs(float(first) - float(second)) <= tolerance + 1e-9


def test_pages_sharing_content_stay_shared_and_text_pages_are_untouched(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    with pikepdf.Pdf.new() as pdf:
        vector = pdf.make_stream(b"0.500000 0.500000 m\n" * 400)
        text = pdf.make_stream(b"BT /F1 12 Tf (Hello 1.500000 2.000000) Tj ET\n" * 200)
        for contents in (vector, vector, text):
            page = pdf.add_blank_page()
            page.obj["/Contents"] = contents
        pdf.save(source)

    with pikepdf.Pdf.open(source) as pdf:
        stats = optimize_content_streams(pdf, CompressionLevel.STRONG)
        assert stats.pages_rewritten == 2
        assert pdf.pages[0].Contents.objgen == pdf.pages[1].Contents.objgen
        assert b"(Hello 1.500000 2.000000)" in pdf.pages[2].Contents.read_bytes()
