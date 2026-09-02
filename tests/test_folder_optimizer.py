from __future__ import annotations

import threading
from pathlib import Path

import pikepdf
import pytest

from pdf_optimizer import (
    CompressionLevel,
    FolderStage,
    OptimizationOptions,
    OptimizationStatus,
    OutputWriteError,
    clone_and_optimize_folder,
    next_available_clone_path,
)


def _write_reducible_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page()
        page.obj["/Contents"] = pdf.make_stream(b"0 0 m 100 100 l S\n" * 20_000)
        pdf.save(
            path,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )


def _write_encrypted_pdf(path: Path) -> None:
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(path, encryption=pikepdf.Encryption(owner="owner", user="secret"))


def test_clone_preserves_tree_and_replaces_pdfs_under_original_names(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Client package"
    nested = source / "2026" / "Reports"
    nested.mkdir(parents=True)
    (source / "Empty folder").mkdir()
    (source / "budget.xlsx").write_bytes(b"excel-placeholder")
    (nested / "brief.docx").write_bytes(b"word-placeholder")
    reducible = nested / "report.pdf"
    protected = source / "protected.pdf"
    _write_reducible_pdf(reducible)
    _write_encrypted_pdf(protected)
    original_reducible = reducible.read_bytes()
    original_protected = protected.read_bytes()

    result = clone_and_optimize_folder(
        source,
        options=OptimizationOptions(compression_level=CompressionLevel.MINIMUM),
    )

    clone = tmp_path / "Client package_optimized"
    assert result.output_path == clone
    assert result.status is OptimizationStatus.OPTIMIZED
    assert result.pdf_total == 2
    assert result.optimized_count == 1
    assert result.copied_pdf_count == 1
    assert result.other_files_copied == 2
    assert (clone / "Empty folder").is_dir()
    assert (clone / "budget.xlsx").read_bytes() == b"excel-placeholder"
    assert (clone / "2026" / "Reports" / "brief.docx").read_bytes() == b"word-placeholder"
    assert (clone / "2026" / "Reports" / "report.pdf").exists()
    assert not (clone / "2026" / "Reports" / "report_optimized.pdf").exists()
    assert (clone / "2026" / "Reports" / "report.pdf").stat().st_size < len(original_reducible)
    assert (clone / "protected.pdf").read_bytes() == original_protected
    assert reducible.read_bytes() == original_reducible
    assert protected.read_bytes() == original_protected
    assert next_available_clone_path(source) == tmp_path / "Client package_optimized_1"


def test_pre_cancel_keeps_no_partial_folder_clone(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "notes.txt").write_text("keep", encoding="utf-8")
    cancel = threading.Event()
    cancel.set()

    result = clone_and_optimize_folder(source, cancel_event=cancel)

    assert result.status is OptimizationStatus.CANCELLED
    assert result.output_path is None
    assert not (tmp_path / "Source_optimized").exists()
    assert not list(tmp_path.glob(".*.pdf_optimizer_clone_*"))
    assert (source / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_cancel_during_copy_removes_staged_clone(tmp_path: Path) -> None:
    source = tmp_path / "Large source"
    source.mkdir()
    (source / "archive.bin").write_bytes(b"data" * 300_000)
    cancel = threading.Event()

    def cancel_when_copying(progress: object) -> None:
        if getattr(progress, "stage", None) is FolderStage.COPYING:
            cancel.set()

    result = clone_and_optimize_folder(
        source,
        cancel_event=cancel,
        progress_callback=cancel_when_copying,
    )

    assert result.status is OptimizationStatus.CANCELLED
    assert result.output_path is None
    assert not (tmp_path / "Large source_optimized").exists()
    assert not list(tmp_path.glob(".*.pdf_optimizer_clone_*"))


def test_clone_rejects_output_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    output = source / "Exports"
    output.mkdir(parents=True)

    with pytest.raises(OutputWriteError, match="outside the source folder"):
        clone_and_optimize_folder(source, output_parent=output)

    assert list(source.rglob("*")) == [output]
