"""Atomic folder cloning with in-place PDF optimization inside the clone."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from .optimizer import (
    CancellationEvent,
    OptimizationError,
    OptimizationOptions,
    OptimizationResult,
    OptimizationStage,
    OptimizationStatus,
    OutputWriteError,
    optimize_pdf,
)
from .smart_images import CompressionLevel


class FolderStage(str, Enum):
    SCANNING = "scanning"
    COPYING = "copying"
    OPTIMIZING_PDF = "optimizing_pdf"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class FolderProgress:
    stage: FolderStage
    current_path: Path | None
    completed_files: int
    total_files: int
    pdf_stage: OptimizationStage | None = None


FolderProgressCallback = Callable[[FolderProgress], None]


@dataclass(frozen=True, slots=True)
class FolderOptimizationResult:
    """Summary of one complete cloned directory tree."""

    source_path: Path
    output_path: Path | None
    status: OptimizationStatus
    input_size: int
    output_size: int
    saved_bytes: int
    saved_percent: float
    page_count: int
    duration: float
    message: str
    pdf_total: int
    optimized_count: int
    already_optimal_count: int
    copied_pdf_count: int
    other_files_copied: int
    images_optimized: int
    compression_level: CompressionLevel


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def next_available_clone_path(
    source_dir: str | os.PathLike[str],
    output_parent: str | os.PathLike[str] | None = None,
    suffix: str = "_optimized",
) -> Path:
    source = _absolute_path(source_dir)
    parent = _absolute_path(output_parent) if output_parent is not None else source.parent
    base_name = f"{source.name}{suffix}"
    index = 0
    while True:
        name = base_name if index == 0 else f"{base_name}_{index}"
        candidate = parent / name
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def _is_relative_to(path: Path, possible_parent: Path) -> bool:
    try:
        path.relative_to(possible_parent)
    except ValueError:
        return False
    return True


def _raise_if_cancelled(cancel_event: CancellationEvent | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _FolderCancelled


class _FolderCancelled(Exception):
    pass


def _emit(callback: FolderProgressCallback | None, progress: FolderProgress) -> None:
    if callback:
        callback(progress)


def _walk_tree(source: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    def raise_walk_error(error: OSError) -> None:
        raise error

    for root, directories, files in os.walk(
        source,
        topdown=True,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        yield Path(root), directories, files


def _copy_file(
    source: Path,
    destination: Path,
    cancel_event: CancellationEvent | None,
) -> None:
    try:
        with source.open("rb") as source_stream, destination.open("xb") as output_stream:
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
        shutil.copystat(source, destination, follow_symlinks=False)
    except _FolderCancelled:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise OutputWriteError(f"Could not copy '{source}'.") from exc


def _copy_symlink(source: Path, destination: Path) -> None:
    try:
        os.symlink(
            os.readlink(source),
            destination,
            target_is_directory=source.is_dir(),
        )
    except OSError as exc:
        raise OutputWriteError(f"Could not preserve the symbolic link '{source}'.") from exc


def _safe_remove_temp_tree(path: Path | None, expected_parent: Path) -> None:
    if path is None or not os.path.lexists(path):
        return
    try:
        resolved_parent = path.resolve(strict=False).parent
        expected = expected_parent.resolve(strict=False)
    except OSError:
        return
    if resolved_parent != expected or ".pdf_optimizer_clone_" not in path.name:
        return
    shutil.rmtree(path, ignore_errors=True)


def _cancelled_result(
    source: Path,
    started_at: float,
    options: OptimizationOptions,
    callback: FolderProgressCallback | None,
    total_files: int = 0,
) -> FolderOptimizationResult:
    _emit(
        callback,
        FolderProgress(FolderStage.CANCELLED, None, 0, total_files),
    )
    return FolderOptimizationResult(
        source_path=source,
        output_path=None,
        status=OptimizationStatus.CANCELLED,
        input_size=0,
        output_size=0,
        saved_bytes=0,
        saved_percent=0.0,
        page_count=0,
        duration=time.perf_counter() - started_at,
        message="Folder cloning was canceled; no partial clone was kept.",
        pdf_total=0,
        optimized_count=0,
        already_optimal_count=0,
        copied_pdf_count=0,
        other_files_copied=0,
        images_optimized=0,
        compression_level=options.compression_level,
    )


def clone_and_optimize_folder(
    source_dir: str | os.PathLike[str],
    output_parent: str | os.PathLike[str] | None = None,
    options: OptimizationOptions | None = None,
    cancel_event: CancellationEvent | None = None,
    progress_callback: FolderProgressCallback | None = None,
) -> FolderOptimizationResult:
    """Clone a full tree and replace each PDF in the clone with an optimized one.

    Every non-PDF file and empty directory is preserved. PDFs that cannot be
    rewritten safely (for example signed, encrypted, or malformed documents)
    are copied byte-for-byte so the clone remains complete. Work is staged in
    a temporary sibling directory and published only when the whole clone is
    ready.
    """
    started_at = time.perf_counter()
    source = _absolute_path(source_dir)
    selected_options = options or OptimizationOptions()
    parent = _absolute_path(output_parent) if output_parent is not None else source.parent
    temporary_root: Path | None = None

    if not source.exists() or not source.is_dir():
        raise OutputWriteError(f"The selected folder does not exist: '{source}'.")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(f"Could not create the output folder '{parent}'.") from exc
    if not parent.is_dir():
        raise OutputWriteError(f"The output location is not a folder: '{parent}'.")
    if _is_relative_to(parent.resolve(strict=False), source.resolve(strict=False)):
        raise OutputWriteError(
            "Choose an output location outside the source folder to avoid recursive cloning."
        )

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result(source, started_at, selected_options, progress_callback)

    _emit(progress_callback, FolderProgress(FolderStage.SCANNING, None, 0, 0))
    try:
        walk_snapshot = list(_walk_tree(source))
    except OSError as exc:
        raise OutputWriteError(f"Could not read every item in '{source}'.") from exc
    total_files = sum(len(files) for _, _, files in walk_snapshot)
    _raise_if_cancelled(cancel_event)

    try:
        temporary_root = Path(
            tempfile.mkdtemp(
                prefix=f".{source.name}.pdf_optimizer_clone_",
                dir=parent,
            )
        )
    except OSError as exc:
        raise OutputWriteError(f"Could not prepare a clone in '{parent}'.") from exc

    pdf_total = 0
    optimized_count = 0
    already_optimal_count = 0
    copied_pdf_count = 0
    other_files_copied = 0
    input_pdf_bytes = 0
    output_pdf_bytes = 0
    total_pages = 0
    images_optimized = 0
    completed_files = 0

    try:
        for root, directories, files in walk_snapshot:
            _raise_if_cancelled(cancel_event)
            relative_root = root.relative_to(source)
            destination_root = temporary_root / relative_root
            destination_root.mkdir(parents=True, exist_ok=True)

            for directory_name in tuple(directories):
                source_directory = root / directory_name
                destination_directory = destination_root / directory_name
                if source_directory.is_symlink():
                    _copy_symlink(source_directory, destination_directory)
                    directories.remove(directory_name)
                else:
                    destination_directory.mkdir(exist_ok=True)

            for filename in files:
                _raise_if_cancelled(cancel_event)
                source_file = root / filename
                destination_file = destination_root / filename
                relative_file = source_file.relative_to(source)
                if source_file.is_symlink():
                    stage = FolderStage.COPYING
                    _emit(
                        progress_callback,
                        FolderProgress(stage, relative_file, completed_files, total_files),
                    )
                    _copy_symlink(source_file, destination_file)
                    other_files_copied += 1
                elif source_file.suffix.lower() == ".pdf":
                    pdf_total += 1
                    try:
                        input_pdf_bytes += source_file.stat().st_size
                    except OSError as exc:
                        raise OutputWriteError(f"Could not inspect '{source_file}'.") from exc

                    def report_pdf_stage(stage: OptimizationStage) -> None:
                        _emit(
                            progress_callback,
                            FolderProgress(
                                FolderStage.OPTIMIZING_PDF,
                                relative_file,
                                completed_files,
                                total_files,
                                stage,
                            ),
                        )

                    try:
                        pdf_result: OptimizationResult = optimize_pdf(
                            source_file,
                            output_dir=destination_root,
                            options=replace(selected_options, suffix=""),
                            cancel_event=cancel_event,
                            progress_callback=report_pdf_stage,
                        )
                    except OptimizationError:
                        _copy_file(source_file, destination_file, cancel_event)
                        copied_pdf_count += 1
                        output_pdf_bytes += destination_file.stat().st_size
                    else:
                        if pdf_result.status is OptimizationStatus.CANCELLED:
                            raise _FolderCancelled
                        if pdf_result.output_path is None:
                            _copy_file(source_file, destination_file, cancel_event)
                            copied_pdf_count += 1
                            output_pdf_bytes += destination_file.stat().st_size
                        else:
                            if pdf_result.output_path != destination_file:
                                raise OutputWriteError(
                                    f"Could not preserve the PDF filename '{relative_file}'."
                                )
                            output_pdf_bytes += pdf_result.output_size
                            total_pages += pdf_result.page_count
                            images_optimized += pdf_result.images_optimized
                            if pdf_result.status is OptimizationStatus.OPTIMIZED:
                                optimized_count += 1
                            else:
                                already_optimal_count += 1
                else:
                    _emit(
                        progress_callback,
                        FolderProgress(
                            FolderStage.COPYING,
                            relative_file,
                            completed_files,
                            total_files,
                        ),
                    )
                    _copy_file(source_file, destination_file, cancel_event)
                    other_files_copied += 1
                completed_files += 1

        # Restore directory timestamps/permissions after writing their contents.
        for root, _, _ in reversed(walk_snapshot):
            destination_root = temporary_root / root.relative_to(source)
            try:
                shutil.copystat(root, destination_root, follow_symlinks=False)
            except OSError as exc:
                raise OutputWriteError(f"Could not preserve folder metadata for '{root}'.") from exc

        _raise_if_cancelled(cancel_event)
        _emit(
            progress_callback,
            FolderProgress(FolderStage.FINALIZING, None, completed_files, total_files),
        )
        final_root = next_available_clone_path(source, parent)
        try:
            os.rename(temporary_root, final_root)
        except OSError as exc:
            raise OutputWriteError(f"Could not finalize the cloned folder '{final_root}'.") from exc
        temporary_root = None

        saved_bytes = max(0, input_pdf_bytes - output_pdf_bytes)
        status = (
            OptimizationStatus.OPTIMIZED
            if saved_bytes > 0
            else OptimizationStatus.ALREADY_OPTIMAL
        )
        result = FolderOptimizationResult(
            source_path=source,
            output_path=final_root,
            status=status,
            input_size=input_pdf_bytes,
            output_size=output_pdf_bytes,
            saved_bytes=saved_bytes,
            saved_percent=(saved_bytes / input_pdf_bytes * 100.0) if input_pdf_bytes else 0.0,
            page_count=total_pages,
            duration=time.perf_counter() - started_at,
            message=(
                f"Cloned the complete folder: {pdf_total} PDF"
                f"{'s' if pdf_total != 1 else ''}, {other_files_copied} other file"
                f"{'s' if other_files_copied != 1 else ''}."
            ),
            pdf_total=pdf_total,
            optimized_count=optimized_count,
            already_optimal_count=already_optimal_count,
            copied_pdf_count=copied_pdf_count,
            other_files_copied=other_files_copied,
            images_optimized=images_optimized,
            compression_level=selected_options.compression_level,
        )
        _emit(
            progress_callback,
            FolderProgress(FolderStage.COMPLETE, None, completed_files, total_files),
        )
        return result
    except _FolderCancelled:
        return _cancelled_result(
            source,
            started_at,
            selected_options,
            progress_callback,
            total_files,
        )
    finally:
        _safe_remove_temp_tree(temporary_root, parent)


__all__ = [
    "FolderOptimizationResult",
    "FolderProgress",
    "FolderProgressCallback",
    "FolderStage",
    "clone_and_optimize_folder",
    "next_available_clone_path",
]
