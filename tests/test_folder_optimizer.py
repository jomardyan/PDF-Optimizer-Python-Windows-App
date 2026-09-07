from __future__ import annotations

import threading
from pathlib import Path
import stat
import sys

import pikepdf
import pytest
import pdf_optimizer.folder_optimizer as folder_module

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


@pytest.mark.parametrize("stage", [FolderStage.SCANNING, FolderStage.FINALIZING])
def test_cancellation_at_scan_and_publish_leaves_no_clone(tmp_path: Path, stage) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "notes.txt").write_text("keep")
    cancel = threading.Event()

    def report(progress):
        if progress.stage is stage:
            cancel.set()

    result = clone_and_optimize_folder(source, cancel_event=cancel, progress_callback=report)
    assert result.status is OptimizationStatus.CANCELLED
    assert list(tmp_path.iterdir()) == [source]


def test_invalid_output_does_not_create_directories_in_source(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    with pytest.raises(OutputWriteError, match="outside"):
        clone_and_optimize_folder(source, source / "new" / "output")
    assert list(source.iterdir()) == []


def test_failed_exclusive_copy_keeps_existing_destination(tmp_path: Path) -> None:
    source, output = tmp_path / "input", tmp_path / "output"
    source.write_bytes(b"new")
    output.write_bytes(b"keep")
    with pytest.raises(OutputWriteError):
        folder_module._copy_file(source, output, None)
    assert output.read_bytes() == b"keep"


def test_cancellation_removes_readonly_staged_files(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    readonly = source / "readonly.txt"
    readonly.write_text("keep")
    readonly.chmod(stat.S_IREAD)
    cancel = threading.Event()

    def report(progress):
        if progress.stage is FolderStage.FINALIZING:
            cancel.set()

    try:
        result = clone_and_optimize_folder(source, cancel_event=cancel, progress_callback=report)
        assert result.status is OptimizationStatus.CANCELLED
        assert list(tmp_path.iterdir()) == [source]
        assert readonly.read_text() == "keep"
    finally:
        readonly.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_publish_retries_concurrent_name_collision(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "notes.txt").write_text("keep")
    original_next = folder_module.next_available_clone_path
    raced = False

    def collide(*args, **kwargs):
        nonlocal raced
        path = original_next(*args, **kwargs)
        if not raced:
            raced = True
            path.mkdir()
            (path / "existing.txt").write_text("do not overwrite")
        return path

    monkeypatch.setattr(folder_module, "next_available_clone_path", collide)
    result = clone_and_optimize_folder(source)
    assert result.output_path.name == "Source_optimized_1"
    assert (tmp_path / "Source_optimized" / "existing.txt").read_text() == "do not overwrite"


@pytest.mark.parametrize("suffix", ["/outside", "\\outside", "\0"])
def test_clone_name_rejects_path_separators(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ValueError):
        next_available_clone_path(tmp_path / "Source", suffix=suffix)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions")
def test_clone_rejects_junction_before_traversal(tmp_path: Path) -> None:
    import _winapi

    source = tmp_path / "Source"
    source.mkdir()
    junction = source / "loop"
    _winapi.CreateJunction(str(source), str(junction))
    try:
        with pytest.raises(OutputWriteError, match="directory junction"):
            clone_and_optimize_folder(source)
        assert not list(tmp_path.glob("Source_optimized*"))
    finally:
        # Remove the link itself, never its target.
        junction.rmdir()
