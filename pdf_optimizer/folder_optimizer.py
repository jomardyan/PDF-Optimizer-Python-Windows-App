"""Atomic folder cloning with in-place PDF optimization inside the clone."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .optimizer import (
    CancellationEvent,
    OptimizationOptions,
    OptimizationResult,
    OptimizationStage,
    OptimizationStatus,
    OutputWriteError,
    _validate_suffix,
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
    _validate_suffix(suffix)
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
        # os.walk(followlinks=False) can still follow Windows junctions.
        for name in directories:
            entry = Path(root) / name
            if getattr(entry.lstat(), "st_reparse_tag", None) == getattr(
                stat, "IO_REPARSE_TAG_MOUNT_POINT", -1
            ):
                raise OutputWriteError(
                    f"Cannot safely clone the directory junction '{entry}'. "
                    "Select its target folder directly instead."
                )
        yield Path(root), directories, files


def _copy_file(
    source: Path,
    destination: Path,
    cancel_event: CancellationEvent | None,
) -> None:
    created = False
    try:
        with source.open("rb") as source_stream, destination.open("xb") as output_stream:
            created = True
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
        shutil.copystat(source, destination, follow_symlinks=False)
    except _FolderCancelled:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise OutputWriteError(f"Could not copy '{source}'.") from exc


def _preserve_file_metadata(source: Path, destination: Path) -> None:
    """Carry timestamps over to a rewritten PDF; failure must not fail the clone."""
    try:
        shutil.copystat(source, destination, follow_symlinks=False)
    except OSError:
        pass


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
    def remove_readonly(function: Callable, filename: str, _error: object) -> None:
        # copystat can make staged files read-only on Windows.
        try:
            os.chmod(filename, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(filename)
        except OSError:
            pass

    shutil.rmtree(path, onerror=remove_readonly)


def _publish_clone(temporary_root: Path, source: Path, parent: Path) -> Path:
    """Publish without overwriting existing clones, including concurrent jobs."""
    while True:
        final_root = next_available_clone_path(source, parent)
        reserved = False
        try:
            if os.name != "nt":
                # POSIX rename replaces empty directories. Reserve our own first.
                final_root.mkdir()
                reserved = True
            os.rename(temporary_root, final_root)
            return final_root
        except OSError as exc:
            if reserved:
                final_root.rmdir()
            elif os.path.lexists(final_root):
                continue
            raise OutputWriteError(
                f"Could not finalize the cloned folder '{final_root}'."
            ) from exc


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


DEFAULT_MAX_WORKERS = 4
_POLL_SECONDS = 0.1
_TEMP_PDF_PATTERN = ".pdf_optimizer_*.pdf"


def default_worker_count() -> int:
    """A few PDFs at once: enough to use several cores, light on memory."""
    return max(1, min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1))


@dataclass(frozen=True, slots=True)
class _PdfTask:
    source_file: Path
    destination_file: Path
    relative_file: Path
    options: OptimizationOptions


@dataclass(frozen=True, slots=True)
class _PdfOutcome:
    """What happened to one PDF; ``result`` is None when it was copied unchanged."""

    result: OptimizationResult | None
    output_size: int


def _process_pdf(
    task: _PdfTask,
    cancel_event: CancellationEvent | None,
    progress_callback: Callable[[OptimizationStage], None] | None,
) -> _PdfOutcome:
    """Optimize one PDF into the clone, or copy it unchanged if that is not possible."""
    try:
        result: OptimizationResult | None = optimize_pdf(
            task.source_file,
            output_dir=task.destination_file.parent,
            options=replace(task.options, suffix=""),
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
    except Exception:  # noqa: BLE001 - one bad PDF must not abort the clone
        # Expected failures (encrypted, malformed, failed validation) and
        # unexpected library errors alike fall back to an exact copy.
        result = None
    if result is not None:
        if result.status is OptimizationStatus.CANCELLED:
            raise _FolderCancelled
        if result.output_path is not None:
            if result.output_path != task.destination_file:
                raise OutputWriteError(
                    f"Could not preserve the PDF filename '{task.relative_file}'."
                )
            _preserve_file_metadata(task.source_file, task.destination_file)
            return _PdfOutcome(result, result.output_size)
    _copy_file(task.source_file, task.destination_file, cancel_event)
    return _PdfOutcome(None, task.destination_file.stat().st_size)


# Worker-process side -------------------------------------------------------

_worker_cancel_event: Any = None


def _init_worker(cancel_event: Any) -> None:
    global _worker_cancel_event
    _worker_cancel_event = cancel_event


class _WorkerCancel:
    def is_set(self) -> bool:
        return _worker_cancel_event is not None and _worker_cancel_event.is_set()


def _process_pdf_in_worker(task: _PdfTask) -> _PdfOutcome:
    return _process_pdf(task, _WorkerCancel(), None)


# Main-process side ---------------------------------------------------------


class _PdfWorkers:
    """A few spawned processes optimizing PDFs, tolerant of worker crashes.

    Processes (not threads) are used because qpdf validation and PDF parsing
    hold the GIL, so threads gave no speed-up.
    """

    def __init__(self, size: int, cancel_event: CancellationEvent | None) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._size = size
        self._cancel_event = cancel_event
        self._stop = self._context.Event()
        self._executor: ProcessPoolExecutor | None = None
        self._pending: dict[Future[_PdfOutcome], _PdfTask] = {}

    @property
    def has_free_slot(self) -> bool:
        return len(self._pending) < self._size

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def _new_executor(self, size: int) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=size,
            mp_context=self._context,
            initializer=_init_worker,
            initargs=(self._stop,),
        )

    def submit(self, task: _PdfTask) -> None:
        for attempt in range(2):
            if self._executor is None:
                self._executor = self._new_executor(self._size)
            try:
                future = self._executor.submit(_process_pdf_in_worker, task)
            except BrokenProcessPool:
                self._discard_executor()
                if attempt:
                    raise OutputWriteError("The PDF worker processes could not be started.")
            else:
                self._pending[future] = task
                return

    def collect(self) -> list[_PdfOutcome]:
        """Wait briefly for finished PDFs; raises a worker's fatal error."""
        done, _ = wait(self._pending, timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
        outcomes: list[_PdfOutcome] = []
        lost: list[_PdfTask] = []
        for future in done:
            task = self._pending.pop(future)
            try:
                outcomes.append(future.result())
            except BrokenProcessPool:
                lost.append(task)
        if lost:
            # The pool is unusable and every other in-flight task is lost with
            # it, so settle them all before anything new is submitted.
            for future, task in list(self._pending.items()):
                del self._pending[future]
                try:
                    outcomes.append(future.result())
                except BrokenProcessPool:
                    lost.append(task)
            self._discard_executor()
            for task in lost:
                outcomes.append(self._recover(task))
        return outcomes

    def _discard_executor(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def _recover(self, task: _PdfTask) -> _PdfOutcome:
        """Retry a lost PDF alone; if it kills its worker again, copy it unchanged."""
        directory = task.destination_file.parent
        # A killed worker can leave its reserved output or a temp file behind.
        task.destination_file.unlink(missing_ok=True)
        for leftover in directory.glob(_TEMP_PDF_PATTERN):
            leftover.unlink(missing_ok=True)
        with self._new_executor(1) as isolated:
            try:
                return isolated.submit(_process_pdf_in_worker, task).result()
            except BrokenProcessPool:
                pass
        task.destination_file.unlink(missing_ok=True)
        for leftover in directory.glob(_TEMP_PDF_PATTERN):
            leftover.unlink(missing_ok=True)
        _copy_file(task.source_file, task.destination_file, self._cancel_event)
        return _PdfOutcome(None, task.destination_file.stat().st_size)

    def close(self) -> None:
        """Stop in-flight work and wait for it so the staged tree can be removed."""
        self._stop.set()
        self._discard_executor()


def clone_and_optimize_folder(
    source_dir: str | os.PathLike[str],
    output_parent: str | os.PathLike[str] | None = None,
    options: OptimizationOptions | None = None,
    cancel_event: CancellationEvent | None = None,
    progress_callback: FolderProgressCallback | None = None,
    max_workers: int | None = None,
) -> FolderOptimizationResult:
    """Clone a full tree and replace each PDF in the clone with an optimized one.

    Every non-PDF file and empty directory is preserved. PDFs that cannot be
    rewritten safely (for example signed, encrypted, or malformed documents)
    are copied byte-for-byte so the clone remains complete. Work is staged in
    a temporary sibling directory and published only when the whole clone is
    ready. Up to ``max_workers`` PDFs (default: a few) are optimized at once in
    separate processes; ``max_workers=1`` processes them one at a time.
    """
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be at least 1.")
    started_at = time.perf_counter()
    source = _absolute_path(source_dir)
    selected_options = options or OptimizationOptions()
    parent = _absolute_path(output_parent) if output_parent is not None else source.parent
    temporary_root: Path | None = None

    if not source.exists() or not source.is_dir():
        raise OutputWriteError(f"The selected folder does not exist: '{source}'.")
    if _is_relative_to(parent.resolve(strict=False), source.resolve(strict=False)):
        raise OutputWriteError(
            "Choose an output location outside the source folder to avoid recursive cloning."
        )
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result(source, started_at, selected_options, progress_callback)
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(f"Could not create the output folder '{parent}'.") from exc
    if not parent.is_dir():
        raise OutputWriteError(f"The output location is not a folder: '{parent}'.")
    try:
        _emit(progress_callback, FolderProgress(FolderStage.SCANNING, None, 0, 0))
        _raise_if_cancelled(cancel_event)
        walk_snapshot = []
        for entry in _walk_tree(source):
            _raise_if_cancelled(cancel_event)
            walk_snapshot.append(entry)
        _raise_if_cancelled(cancel_event)
    except _FolderCancelled:
        return _cancelled_result(source, started_at, selected_options, progress_callback)
    except OSError as exc:
        raise OutputWriteError(f"Could not read every item in '{source}'.") from exc
    total_files = sum(len(files) for _, _, files in walk_snapshot)

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

    pdf_candidates = sum(
        1
        for root, _, files in walk_snapshot
        for filename in files
        if filename.lower().endswith(".pdf") and not (root / filename).is_symlink()
    )
    worker_count = min(max_workers or default_worker_count(), pdf_candidates)
    # A single worker (or a single PDF) is processed inline: no process start-up.
    workers = _PdfWorkers(worker_count, cancel_event) if worker_count > 1 else None

    def record(outcome: _PdfOutcome) -> None:
        nonlocal optimized_count, already_optimal_count, copied_pdf_count
        nonlocal output_pdf_bytes, total_pages, images_optimized, completed_files
        output_pdf_bytes += outcome.output_size
        result = outcome.result
        if result is None:
            copied_pdf_count += 1
        else:
            total_pages += result.page_count
            images_optimized += result.images_optimized
            if result.status is OptimizationStatus.OPTIMIZED:
                optimized_count += 1
            else:
                already_optimal_count += 1
        completed_files += 1

    def drain(until_free_slot: bool) -> None:
        while workers is not None and workers.has_pending:
            if until_free_slot and workers.has_free_slot:
                return
            for outcome in workers.collect():
                record(outcome)
            _raise_if_cancelled(cancel_event)

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
                if source_file.suffix.lower() == ".pdf" and not source_file.is_symlink():
                    pdf_total += 1
                    try:
                        input_pdf_bytes += source_file.stat().st_size
                    except OSError as exc:
                        raise OutputWriteError(f"Could not inspect '{source_file}'.") from exc
                    task = _PdfTask(source_file, destination_file, relative_file, selected_options)

                    if workers is None:

                        def report_pdf_stage(
                            stage: OptimizationStage,
                            current_file: Path = relative_file,
                        ) -> None:
                            _emit(
                                progress_callback,
                                FolderProgress(
                                    FolderStage.OPTIMIZING_PDF,
                                    current_file,
                                    completed_files,
                                    total_files,
                                    stage,
                                ),
                            )

                        record(_process_pdf(task, cancel_event, report_pdf_stage))
                    else:
                        # Wait for a free worker slot; this also bounds memory use.
                        drain(until_free_slot=True)
                        _emit(
                            progress_callback,
                            FolderProgress(
                                FolderStage.OPTIMIZING_PDF,
                                relative_file,
                                completed_files,
                                total_files,
                            ),
                        )
                        workers.submit(task)
                    continue

                _emit(
                    progress_callback,
                    FolderProgress(
                        FolderStage.COPYING, relative_file, completed_files, total_files
                    ),
                )
                if source_file.is_symlink():
                    _copy_symlink(source_file, destination_file)
                else:
                    _copy_file(source_file, destination_file, cancel_event)
                other_files_copied += 1
                completed_files += 1

        drain(until_free_slot=False)

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
        _raise_if_cancelled(cancel_event)
        final_root = _publish_clone(temporary_root, source, parent)
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
    except OSError as exc:
        raise OutputWriteError(f"Could not complete the clone of '{source}'.") from exc
    finally:
        if workers is not None:
            # Workers write inside the staged tree: stop them before cleanup.
            workers.close()
        _safe_remove_temp_tree(temporary_root, parent)


__all__ = [
    "FolderOptimizationResult",
    "FolderProgress",
    "FolderProgressCallback",
    "FolderStage",
    "clone_and_optimize_folder",
    "default_worker_count",
    "next_available_clone_path",
]
