from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

import pdf_optimizer.optimizer as optimizer_module
from pdf_optimizer import (
    EncryptedPDFError,
    InvalidPDFError,
    OptimizationOptions,
    OptimizationStage,
    OptimizationStatus,
    OutputWriteError,
    next_available_output_path,
    optimize_pdf,
)


def _write_feature_pdf(path: Path) -> tuple[bytes, bytes]:
    content = (
        b"q 0.25 0 0 0.25 0 0 cm 0 0 100 100 re S Q\n" * 25_000
        + b"q 32 0 0 32 200 200 cm /Im0 Do Q\n"
    )
    jpeg_buffer = BytesIO()
    Image.new("RGB", (2, 2), (23, 117, 201)).save(
        jpeg_buffer, format="JPEG", quality=82
    )
    jpeg_bytes = jpeg_buffer.getvalue()
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.obj["/Contents"] = pdf.make_stream(content)
        image = pdf.make_stream(jpeg_bytes)
        image["/Type"] = pikepdf.Name.XObject
        image["/Subtype"] = pikepdf.Name.Image
        image["/Width"] = 2
        image["/Height"] = 2
        image["/ColorSpace"] = pikepdf.Name.DeviceRGB
        image["/BitsPerComponent"] = 8
        image["/Filter"] = pikepdf.Name.DCTDecode
        page.obj["/Resources"] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary({"/Im0": image})
        )
        pdf.docinfo["/Title"] = "Lossless fixture"
        pdf.docinfo["/Author"] = "PDF Optimizer tests"

        link = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot,
                Subtype=pikepdf.Name.Link,
                Rect=pikepdf.Array([20, 20, 180, 45]),
                Border=pikepdf.Array([0, 0, 0]),
                A=pikepdf.Dictionary(
                    S=pikepdf.Name.URI,
                    URI=pikepdf.String("https://example.com/"),
                ),
            )
        )
        page.obj["/Annots"] = pikepdf.Array([link])

        field = pdf.make_indirect(
            pikepdf.Dictionary(
                FT=pikepdf.Name.Tx,
                T=pikepdf.String("preserved-field"),
                V=pikepdf.String("preserved value"),
            )
        )
        pdf.Root["/AcroForm"] = pdf.make_indirect(
            pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        )

        with pdf.open_outline() as outline:
            outline.root.append(pikepdf.OutlineItem("Start", 0))

        pdf.save(
            path,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )
    return content, jpeg_bytes


def _write_small_pdf(path: Path) -> None:
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(72, 72))
        pdf.save(path)


def _write_signed_pdf(path: Path) -> None:
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page()
        signature = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Sig,
                ByteRange=pikepdf.Array([0, 10, 20, 10]),
                Contents=pikepdf.String(b"not-a-real-cryptographic-signature"),
            )
        )
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                FT=pikepdf.Name.Sig,
                T=pikepdf.String("Signature1"),
                V=signature,
            )
        )
        pdf.Root["/AcroForm"] = pdf.make_indirect(
            pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        )
        pdf.save(path)


def test_lossless_optimization_shrinks_and_preserves_features(tmp_path: Path) -> None:
    source = tmp_path / "feature-rich.pdf"
    expected_content, expected_jpeg = _write_feature_pdf(source)
    original_bytes = source.read_bytes()

    stages: list[OptimizationStage] = []
    result = optimize_pdf(source, progress_callback=stages.append)

    assert result.status is OptimizationStatus.OPTIMIZED
    assert result.source_path == source
    assert result.output_path == tmp_path / "feature-rich_optimized.pdf"
    assert result.output_path.exists()
    assert result.input_size == len(original_bytes)
    assert result.output_size == result.output_path.stat().st_size
    assert 0 < result.saved_bytes == result.input_size - result.output_size
    assert result.saved_percent == pytest.approx(
        result.saved_bytes / result.input_size * 100
    )
    assert result.page_count == 1
    assert result.duration >= 0
    assert stages == [
        OptimizationStage.CHECKING,
        OptimizationStage.OPTIMIZING,
        OptimizationStage.VERIFYING,
        OptimizationStage.FINALIZING,
        OptimizationStage.COMPLETE,
    ]
    assert source.read_bytes() == original_bytes

    with pikepdf.Pdf.open(result.output_path, attempt_recovery=False) as output:
        assert len(output.pages) == 1
        assert output.pages[0].obj["/Contents"].read_bytes() == expected_content
        assert (
            output.pages[0].obj["/Resources"]["/XObject"]["/Im0"].read_raw_bytes()
            == expected_jpeg
        )
        assert str(output.docinfo["/Title"]) == "Lossless fixture"
        assert str(output.docinfo["/Author"]) == "PDF Optimizer tests"
        assert len(output.pages[0].obj["/Annots"]) == 1
        assert str(output.pages[0].obj["/Annots"][0]["/Subtype"]) == "/Link"
        assert "/AcroForm" in output.Root
        assert str(output.Root["/AcroForm"]["/Fields"][0]["/V"]) == "preserved value"
        with output.open_outline() as outline:
            assert [item.title for item in outline.root] == ["Start"]


def test_non_smaller_candidate_is_replaced_by_exact_copy(tmp_path: Path) -> None:
    source = tmp_path / "small.pdf"
    _write_small_pdf(source)
    original_bytes = source.read_bytes()

    # Linearization has fixed overhead and reliably makes this tiny PDF larger.
    result = optimize_pdf(source, options=OptimizationOptions(linearize=True))

    assert result.status is OptimizationStatus.ALREADY_OPTIMAL
    assert result.output_path == tmp_path / "small_optimized.pdf"
    assert result.output_path.read_bytes() == original_bytes
    assert result.output_size == result.input_size == len(original_bytes)
    assert result.saved_bytes == 0
    assert result.saved_percent == 0
    assert "Already optimal" in result.message


def test_output_paths_are_unique_and_never_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    _write_small_pdf(source)
    original = source.read_bytes()
    collision = tmp_path / "report_optimized.pdf"
    collision.write_bytes(b"keep me")

    assert next_available_output_path(source) == tmp_path / "report_optimized_1.pdf"
    first = optimize_pdf(source, options=OptimizationOptions(linearize=True))
    second = optimize_pdf(source, options=OptimizationOptions(linearize=True))

    assert first.output_path == tmp_path / "report_optimized_1.pdf"
    assert second.output_path == tmp_path / "report_optimized_2.pdf"
    assert first.output_path.read_bytes() == original
    assert second.output_path.read_bytes() == original
    assert source.read_bytes() == original
    assert collision.read_bytes() == b"keep me"


def test_invalid_pdf_has_friendly_error_and_creates_no_output(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    original = b"this is not a PDF"
    source.write_bytes(original)

    with pytest.raises(InvalidPDFError, match="not a valid readable PDF"):
        optimize_pdf(source)

    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


def test_encrypted_pdf_has_friendly_error_and_stays_untouched(tmp_path: Path) -> None:
    source = tmp_path / "protected.pdf"
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(
            source,
            encryption=pikepdf.Encryption(owner="owner password", user="secret"),
        )
    original = source.read_bytes()

    with pytest.raises(EncryptedPDFError, match="password protected"):
        optimize_pdf(source)

    assert source.read_bytes() == original
    assert not (tmp_path / "protected_optimized.pdf").exists()


def test_signed_pdf_is_skipped_without_writing_output(tmp_path: Path) -> None:
    source = tmp_path / "signed.pdf"
    _write_signed_pdf(source)
    original = source.read_bytes()
    stages: list[OptimizationStage] = []

    result = optimize_pdf(source, progress_callback=stages.append)

    assert result.status is OptimizationStatus.SIGNED_SKIPPED
    assert result.output_path is None
    assert result.page_count == 1
    assert result.saved_bytes == 0
    assert "digitally signed" in result.message
    assert stages == [
        OptimizationStage.CHECKING,
        OptimizationStage.SIGNED_SKIPPED,
    ]
    assert source.read_bytes() == original
    assert not (tmp_path / "signed_optimized.pdf").exists()


def test_unsigned_signature_field_is_not_mistaken_for_signed_pdf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsigned-field.pdf"
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page()
        empty_field = pdf.make_indirect(
            pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Empty"))
        )
        pdf.Root["/AcroForm"] = pdf.make_indirect(
            pikepdf.Dictionary(Fields=pikepdf.Array([empty_field]))
        )
        pdf.save(source)

    result = optimize_pdf(source, options=OptimizationOptions(linearize=True))

    assert result.status is not OptimizationStatus.SIGNED_SKIPPED
    assert result.output_path is not None


def test_cancellation_from_stage_callback_leaves_no_partial_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cancel.pdf"
    _write_feature_pdf(source)
    original = source.read_bytes()
    event = threading.Event()
    stages: list[OptimizationStage] = []

    def cancel_when_optimization_starts(stage: OptimizationStage) -> None:
        stages.append(stage)
        if stage is OptimizationStage.OPTIMIZING:
            event.set()

    result = optimize_pdf(
        source,
        cancel_event=event,
        progress_callback=cancel_when_optimization_starts,
    )

    assert result.status is OptimizationStatus.CANCELLED
    assert result.output_path is None
    assert stages == [
        OptimizationStage.CHECKING,
        OptimizationStage.OPTIMIZING,
        OptimizationStage.CANCELLED,
    ]
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".pdf_optimizer_*.pdf"))
    assert not (tmp_path / "cancel_optimized.pdf").exists()


def test_cancellation_after_candidate_write_removes_temporary_pdf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cancel-after-write.pdf"
    _write_feature_pdf(source)
    event = threading.Event()
    stages: list[OptimizationStage] = []

    def cancel_during_verification(stage: OptimizationStage) -> None:
        stages.append(stage)
        if stage is OptimizationStage.VERIFYING:
            event.set()

    result = optimize_pdf(
        source,
        cancel_event=event,
        progress_callback=cancel_during_verification,
    )

    assert result.status is OptimizationStatus.CANCELLED
    assert stages == [
        OptimizationStage.CHECKING,
        OptimizationStage.OPTIMIZING,
        OptimizationStage.VERIFYING,
        OptimizationStage.CANCELLED,
    ]
    assert not list(tmp_path.glob(".pdf_optimizer_*.pdf"))
    assert not (tmp_path / "cancel-after-write_optimized.pdf").exists()


def test_pre_cancel_does_not_even_open_the_pdf(tmp_path: Path) -> None:
    source = tmp_path / "pre-cancel.pdf"
    source.write_bytes(b"invalid data is irrelevant after pre-cancel")
    event = threading.Event()
    event.set()

    result = optimize_pdf(source, cancel_event=event)

    assert result.status is OptimizationStatus.CANCELLED
    assert result.page_count == 0
    assert result.output_path is None


def test_atomic_finalize_failure_cleans_up_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "atomic.pdf"
    _write_feature_pdf(source)
    original = source.read_bytes()

    def fail_replace(_source: object, _destination: object) -> None:
        raise PermissionError("simulated finalize failure")

    monkeypatch.setattr(optimizer_module, "_atomic_replace", fail_replace)

    with pytest.raises(OutputWriteError, match="Could not finalize"):
        optimize_pdf(source)

    assert source.read_bytes() == original
    assert not (tmp_path / "atomic_optimized.pdf").exists()
    assert not list(tmp_path.glob(".pdf_optimizer_*.pdf"))


def test_direct_nested_signature_is_preserved(tmp_path: Path) -> None:
    source = tmp_path / "direct-signature.pdf"
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page()
        signature = pikepdf.Dictionary(
            Type=pikepdf.Name.Sig, ByteRange=pikepdf.Array([0, 10, 20, 10]),
            Contents=pikepdf.String(b"signature"),
        )
        field = pikepdf.Dictionary(FT=pikepdf.Name.Sig, V=signature)
        pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        pdf.save(source)
    result = optimize_pdf(source)
    assert result.status is OptimizationStatus.SIGNED_SKIPPED
    assert result.output_path is None


def test_cancelled_result_retains_compression_selection(tmp_path: Path) -> None:
    source = tmp_path / "cancel.pdf"
    source.write_bytes(b"irrelevant")
    cancel = threading.Event()
    cancel.set()
    result = optimize_pdf(source, options=OptimizationOptions(compression_level="strong"), cancel_event=cancel)
    assert result.compression_level.value == "strong"


def test_syntax_warnings_reject_candidate_and_clean_output(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "syntax.pdf"
    _write_feature_pdf(source)
    original = source.read_bytes()
    monkeypatch.setattr(pikepdf.Pdf, "check_pdf_syntax", lambda *a, **k: ["damaged stream"])
    with pytest.raises(optimizer_module.ValidationError, match="syntax validation"):
        optimize_pdf(source)
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]
