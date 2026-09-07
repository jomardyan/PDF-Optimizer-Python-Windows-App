"""Content-aware PDF optimization with a strictly lossless default.

Minimum compression changes only PDF structure and lossless streams. Medium
and Strong automatically target large raster images in image-heavy or mixed
documents while leaving text/vector-only documents on the lossless path.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeAlias

import pikepdf

from .smart_images import (
    CompressionLevel,
    DocumentType,
    analyze_document,
    coerce_compression_level,
    document_type_label,
    optimize_raster_images,
)

PathLike: TypeAlias = str | os.PathLike[str]


class CancellationEvent(Protocol):
    """The small part of :class:`threading.Event` used by the optimizer."""

    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


class OptimizationStatus(str, Enum):
    """Terminal state of an optimization attempt."""

    OPTIMIZED = "optimized"
    ALREADY_OPTIMAL = "already_optimal"
    SIGNED_SKIPPED = "signed_skipped"
    CANCELLED = "cancelled"


class OptimizationStage(str, Enum):
    """Coarse progress stages suitable for display in a GUI."""

    CHECKING = "checking"
    OPTIMIZING = "optimizing"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    SIGNED_SKIPPED = "signed_skipped"


ProgressCallback: TypeAlias = Callable[[OptimizationStage], None]


@dataclass(frozen=True, slots=True)
class OptimizationOptions:
    """PDF optimizer settings.

    Minimum is strictly lossless. Medium and Strong permit content-aware JPEG
    re-encoding/downscaling for large raster images, but text, fonts, vectors,
    links, forms, and document structure remain untouched.
    """

    suffix: str = "_optimized"
    recompress_flate: bool = True
    generate_object_streams: bool = True
    linearize: bool = False
    compression_level: CompressionLevel | str = CompressionLevel.MINIMUM

    def __post_init__(self) -> None:
        if any(character in self.suffix for character in ("/", "\\", "\0")):
            raise ValueError("The output filename suffix must not contain a path separator.")
        object.__setattr__(
            self,
            "compression_level",
            coerce_compression_level(self.compression_level),
        )


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Outcome and measurements for one input PDF."""

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
    document_type: DocumentType = DocumentType.TEXT_VECTOR
    images_optimized: int = 0
    compression_level: CompressionLevel = CompressionLevel.MINIMUM

    @property
    def original_size(self) -> int:
        """Compatibility-friendly alias for ``input_size``."""

        return self.input_size

    @property
    def reduction_percent(self) -> float:
        """Compatibility-friendly alias for ``saved_percent``."""

        return self.saved_percent


class OptimizationError(Exception):
    """Base class for errors that are safe to show to an end user."""


class InputPDFError(OptimizationError):
    """The selected input cannot be read as a regular file."""


class InvalidPDFError(InputPDFError):
    """The selected input is not a valid readable PDF."""


class EncryptedPDFError(InputPDFError):
    """The selected PDF is password protected."""


class SignedPDFError(InputPDFError):
    """Optimization was skipped to avoid invalidating a digital signature."""


class OutputWriteError(OptimizationError):
    """An output file could not be created safely."""


class ValidationError(OptimizationError):
    """The optimized candidate failed post-write validation."""


class _CancellationRequested(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _DocumentInvariants:
    page_count: int
    catalog_features: frozenset[str]
    annotation_counts: tuple[int, ...]
    document_info: tuple[tuple[str, str], ...]


_CATALOG_FEATURE_KEYS = (
    "/AcroForm",
    "/Collection",
    "/Lang",
    "/MarkInfo",
    "/Metadata",
    "/Names",
    "/OCProperties",
    "/OpenAction",
    "/Outlines",
    "/OutputIntents",
    "/PageLabels",
    "/StructTreeRoot",
    "/ViewerPreferences",
)


def _absolute_path(path: PathLike) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _validate_suffix(suffix: str) -> None:
    if any(character in suffix for character in ("/", "\\", "\0")):
        raise ValueError("The output filename suffix must not contain a path separator.")


def _output_components(input_path: Path, suffix: str) -> tuple[str, str]:
    _validate_suffix(suffix)
    extension = input_path.suffix or ".pdf"
    stem = input_path.stem if input_path.suffix else input_path.name
    return f"{stem}{suffix}", extension


def next_available_output_path(
    input_path: PathLike,
    output_dir: PathLike | None = None,
    suffix: str = "_optimized",
) -> Path:
    """Return the next non-existing ``*_optimized`` output path.

    The function does not create or reserve the returned path.  The optimizer
    performs an additional exclusive reservation immediately before its atomic
    finalize step, so concurrent optimizations cannot overwrite one another.
    """

    source = _absolute_path(input_path)
    directory = _absolute_path(output_dir) if output_dir is not None else source.parent
    base_name, extension = _output_components(source, suffix)

    index = 0
    while True:
        numbered_name = base_name if index == 0 else f"{base_name}_{index}"
        candidate = directory / f"{numbered_name}{extension}"
        # lexists also protects a broken symlink from being overwritten.
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def _reserve_output_path(source: Path, directory: Path, suffix: str) -> Path:
    """Exclusively reserve an output name, retrying if another worker wins."""

    while True:
        candidate = next_available_output_path(source, directory, suffix)
        if candidate.resolve(strict=False) == source.resolve(strict=False):
            # Retain this invariant even with an empty suffix or on unusual
            # filesystems.
            raise OutputWriteError("Refusing to overwrite the original PDF.")
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OutputWriteError(
                f"Could not reserve an output file in '{directory}'."
            ) from exc
        else:
            os.close(descriptor)
            return candidate


def _temporary_pdf_path(directory: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".pdf_optimizer_", suffix=".pdf", dir=directory
        )
    except OSError as exc:
        raise OutputWriteError(
            f"Could not create a temporary file in '{directory}'."
        ) from exc
    os.close(descriptor)
    return Path(name)


def _remove_if_present(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Cleanup is best effort.  Never mask the useful primary error.
        pass


def _files_are_identical(
    first: Path,
    second: Path,
    cancel_event: CancellationEvent | None = None,
) -> bool:
    """Compare two files byte-for-byte without loading a PDF into memory."""

    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as first_file, second.open("rb") as second_file:
            while True:
                _raise_if_cancelled(cancel_event)
                first_chunk = first_file.read(1024 * 1024)
                second_chunk = second_file.read(1024 * 1024)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    return True
    except OSError as exc:
        raise ValidationError("The exact-copy fallback could not be verified.") from exc


def _copy_file(
    source: Path,
    destination: Path,
    cancel_event: CancellationEvent | None,
) -> None:
    """Copy source bytes while retaining responsive, safe cancellation."""

    try:
        with source.open("rb") as source_file, destination.open("wb") as output_file:
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
            output_file.flush()
    except _CancellationRequested:
        raise
    except OSError as exc:
        raise OutputWriteError(
            f"Could not copy the already optimal PDF '{source.name}'."
        ) from exc


def _atomic_replace(source: Path, destination: Path) -> None:
    """Finalize a validated file atomically on the destination filesystem."""

    os.replace(source, destination)


def _emit(callback: ProgressCallback | None, stage: OptimizationStage) -> None:
    if callback is not None:
        callback(stage)


def _raise_if_cancelled(cancel_event: CancellationEvent | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _CancellationRequested


def _cancelled_result(
    source: Path,
    input_size: int,
    page_count: int,
    started_at: float,
    callback: ProgressCallback | None,
    compression_level: CompressionLevel = CompressionLevel.MINIMUM,
) -> OptimizationResult:
    _emit(callback, OptimizationStage.CANCELLED)
    return OptimizationResult(
        source_path=source,
        output_path=None,
        status=OptimizationStatus.CANCELLED,
        input_size=input_size,
        output_size=0,
        saved_bytes=0,
        saved_percent=0.0,
        page_count=page_count,
        duration=time.perf_counter() - started_at,
        message="Optimization canceled; no output file was created.",
        compression_level=compression_level,
    )


def _object_name(value: object) -> str:
    try:
        return str(value)
    except (TypeError, ValueError, RuntimeError):
        return ""


def _looks_like_signature_dictionary(value: object) -> bool:
    try:
        if _object_name(value.get("/Type")) == "/Sig":  # type: ignore[union-attr]
            return True
        return "/ByteRange" in value and "/Contents" in value  # type: ignore[operator]
    except (AttributeError, TypeError, ValueError, RuntimeError, pikepdf.PdfError):
        return False


def _has_digital_signature(
    pdf: pikepdf.Pdf, cancel_event: CancellationEvent | None = None,
) -> bool:
    """Conservatively detect approval, certification, and usage signatures."""

    root = pdf.Root
    try:
        permissions = root.get("/Perms")
        if permissions is not None and any(
            key in permissions for key in ("/DocMDP", "/UR", "/UR3")
        ):
            return True
    except (AttributeError, TypeError, ValueError, RuntimeError, pikepdf.PdfError):
        # The full object scan below remains the authoritative fallback.
        pass

    # Field dictionaries may be direct and nested, so pdf.objects alone is
    # insufficient to protect every signed document.
    pending = list(pdf.objects)
    retained: dict[tuple[int, int] | int, object] = {}
    while pending:
        _raise_if_cancelled(cancel_event)
        obj = pending.pop()
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream)):
            continue
        key = obj.objgen if obj.objgen != (0, 0) else id(obj)
        if key in retained:
            continue
        retained[key] = obj
        if _looks_like_signature_dictionary(obj):
            return True
        try:
            if isinstance(obj, pikepdf.Array):
                pending.extend(obj)
                continue
            pending.extend(obj.values())
            if _object_name(obj.get("/FT")) == "/Sig":
                value = obj.get("/V")
                if value is not None and _looks_like_signature_dictionary(value):
                    return True
        except (AttributeError, TypeError, ValueError, RuntimeError, pikepdf.PdfError):
            continue
    return False


def _document_info_signature(pdf: pikepdf.Pdf) -> tuple[tuple[str, str], ...]:
    try:
        info = pdf.trailer.get("/Info")
        if info is None:
            return ()
        return tuple(sorted((_object_name(key), _object_name(value)) for key, value in info.items()))
    except (AttributeError, TypeError, ValueError, RuntimeError, pikepdf.PdfError):
        return ()


def _capture_invariants(pdf: pikepdf.Pdf) -> _DocumentInvariants:
    catalog_features = frozenset(key for key in _CATALOG_FEATURE_KEYS if key in pdf.Root)
    annotation_counts: list[int] = []
    for page in pdf.pages:
        annotations = page.obj.get("/Annots")
        annotation_counts.append(len(annotations) if annotations is not None else 0)
    return _DocumentInvariants(
        page_count=len(pdf.pages),
        catalog_features=catalog_features,
        annotation_counts=tuple(annotation_counts),
        document_info=_document_info_signature(pdf),
    )


def _validate_candidate(
    path: Path,
    expected: _DocumentInvariants,
    cancel_event: CancellationEvent | None = None,
) -> None:
    try:
        _raise_if_cancelled(cancel_event)
        with pikepdf.Pdf.open(
            path,
            suppress_warnings=True,
            attempt_recovery=False,
            inherit_page_attributes=False,
        ) as candidate:
            # qpdf performs additional syntax checks here (including stream
            # access) beyond merely opening the cross-reference table.
            issues = candidate.check_pdf_syntax(
                progress=lambda _percent: _raise_if_cancelled(cancel_event)
            )
            if issues:
                raise ValidationError(
                    "The optimized PDF failed syntax validation; no output was saved."
                )
            _raise_if_cancelled(cancel_event)
            actual = _capture_invariants(candidate)
            _raise_if_cancelled(cancel_event)
    except _CancellationRequested:
        raise
    except (pikepdf.PdfError, OSError, ValueError) as exc:
        raise ValidationError(
            "The optimized PDF could not be reopened safely; no output was saved."
        ) from exc

    if actual.page_count != expected.page_count:
        raise ValidationError(
            "The optimized PDF did not retain every page; no output was saved."
        )
    if actual.catalog_features != expected.catalog_features:
        raise ValidationError(
            "The optimized PDF did not retain all document features; no output was saved."
        )
    if actual.annotation_counts != expected.annotation_counts:
        raise ValidationError(
            "The optimized PDF did not retain all annotations; no output was saved."
        )
    if actual.document_info != expected.document_info:
        raise ValidationError(
            "The optimized PDF did not retain its metadata; no output was saved."
        )


def _open_input(source: Path) -> pikepdf.Pdf:
    try:
        pdf = pikepdf.Pdf.open(
            source,
            suppress_warnings=True,
            attempt_recovery=False,
            inherit_page_attributes=False,
        )
    except pikepdf.PasswordError as exc:
        raise EncryptedPDFError(
            f"'{source.name}' is password protected and cannot be optimized."
        ) from exc
    except (pikepdf.PdfError, ValueError) as exc:
        raise InvalidPDFError(
            f"'{source.name}' is not a valid readable PDF."
        ) from exc
    except OSError as exc:
        raise InputPDFError(f"Could not read '{source}'.") from exc

    if pdf.is_encrypted:
        pdf.close()
        raise EncryptedPDFError(
            f"'{source.name}' is encrypted and cannot be optimized safely."
        )
    return pdf


def optimize_pdf(
    input_path: PathLike,
    output_dir: PathLike | None = None,
    options: OptimizationOptions | None = None,
    cancel_event: CancellationEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> OptimizationResult:
    """Optimize one PDF according to its content and selected strength.

    Work is written to a temporary PDF, reopened and checked, and only then
    atomically moved to an exclusively reserved output path.  If the optimized
    representation is not smaller, that candidate is discarded and the output
    is an exact byte-for-byte copy of the original.

    Minimum is strictly lossless. Medium and Strong re-encode only qualifying
    raster images in image-heavy/mixed PDFs, leaving text/vector-only PDFs on
    the lossless path. Signed documents are skipped because any rewrite would
    invalidate their cryptographic signature.
    """

    started_at = time.perf_counter()
    source = _absolute_path(input_path)
    selected_options = options or OptimizationOptions()
    # Also validates options passed from code that bypassed normal construction
    # (for example, deserialization helpers).
    _validate_suffix(selected_options.suffix)

    try:
        if not source.exists():
            raise InputPDFError(f"The selected PDF does not exist: '{source}'.")
        if not source.is_file():
            raise InputPDFError(f"The selected PDF is not a file: '{source}'.")
        input_size = source.stat().st_size
    except OptimizationError:
        raise
    except OSError as exc:
        raise InputPDFError(f"Could not read '{source}'.") from exc

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result(
            source, input_size, 0, started_at, progress_callback,
            selected_options.compression_level,
        )

    _emit(progress_callback, OptimizationStage.CHECKING)
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result(
            source, input_size, 0, started_at, progress_callback,
            selected_options.compression_level,
        )

    pdf = _open_input(source)
    page_count = 0
    temporary_path: Path | None = None
    reserved_path: Path | None = None
    try:
        _raise_if_cancelled(cancel_event)
        invariants = _capture_invariants(pdf)
        page_count = invariants.page_count
        profile = analyze_document(
            pdf,
            lambda: _raise_if_cancelled(cancel_event),
        )
        _raise_if_cancelled(cancel_event)

        if _has_digital_signature(pdf, cancel_event):
            error = SignedPDFError(
                f"'{source.name}' is digitally signed; optimization was skipped "
                "to keep the signature valid."
            )
            _emit(progress_callback, OptimizationStage.SIGNED_SKIPPED)
            return OptimizationResult(
                source_path=source,
                output_path=None,
                status=OptimizationStatus.SIGNED_SKIPPED,
                input_size=input_size,
                output_size=0,
                saved_bytes=0,
                saved_percent=0.0,
                page_count=page_count,
                duration=time.perf_counter() - started_at,
                message=str(error),
                document_type=profile.document_type,
                compression_level=selected_options.compression_level,
            )

        _raise_if_cancelled(cancel_event)

        directory = (
            _absolute_path(output_dir) if output_dir is not None else source.parent
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputWriteError(
                f"Could not create the output folder '{directory}'."
            ) from exc
        if not directory.is_dir():
            raise OutputWriteError(f"The output location is not a folder: '{directory}'.")

        _emit(progress_callback, OptimizationStage.OPTIMIZING)
        _raise_if_cancelled(cancel_event)
        temporary_path = _temporary_pdf_path(directory)

        image_stats = optimize_raster_images(
            pdf,
            profile,
            selected_options.compression_level,
            lambda: _raise_if_cancelled(cancel_event),
        )
        _raise_if_cancelled(cancel_event)

        object_stream_mode = (
            pikepdf.ObjectStreamMode.generate
            if selected_options.generate_object_streams
            else pikepdf.ObjectStreamMode.preserve
        )

        def save_progress(_percent: int) -> None:
            _raise_if_cancelled(cancel_event)

        try:
            pdf.save(
                temporary_path,
                preserve_pdfa=True,
                fix_metadata_version=False,
                compress_streams=True,
                stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                object_stream_mode=object_stream_mode,
                normalize_content=False,
                linearize=selected_options.linearize,
                progress=save_progress,
                encryption=False,
                recompress_flate=selected_options.recompress_flate,
            )
        except _CancellationRequested:
            raise
        except (pikepdf.PdfError, OSError, ValueError) as exc:
            raise OutputWriteError(
                f"Could not create an optimized version of '{source.name}'."
            ) from exc
        finally:
            pdf.close()

        _raise_if_cancelled(cancel_event)
        _emit(progress_callback, OptimizationStage.VERIFYING)
        _raise_if_cancelled(cancel_event)
        _validate_candidate(temporary_path, invariants, cancel_event)

        try:
            candidate_size = temporary_path.stat().st_size
        except OSError as exc:
            raise ValidationError(
                "The optimized PDF disappeared before it could be verified."
            ) from exc

        if candidate_size >= input_size:
            # The contract intentionally still creates an output.  It is a true
            # byte-for-byte copy, rather than a larger rewritten PDF.
            _copy_file(source, temporary_path, cancel_event)
            try:
                copied_size = temporary_path.stat().st_size
            except OSError as exc:
                raise ValidationError("The copied PDF could not be verified.") from exc
            if copied_size != input_size:
                raise ValidationError(
                    "The exact-copy fallback changed size; no output was saved."
                )
            if not _files_are_identical(source, temporary_path, cancel_event):
                raise ValidationError(
                    "The exact-copy fallback changed data; no output was saved."
                )
            _validate_candidate(temporary_path, invariants, cancel_event)
            status = OptimizationStatus.ALREADY_OPTIMAL
            output_size = input_size
            saved_bytes = 0
            final_images_optimized = 0
            message = (
                "Already optimal—no smaller version was found at the selected level; "
                "an exact copy was saved."
            )
        else:
            status = OptimizationStatus.OPTIMIZED
            output_size = candidate_size
            saved_bytes = input_size - output_size
            final_images_optimized = image_stats.images_reencoded
            content_label = document_type_label(profile.document_type)
            if final_images_optimized:
                message = (
                    f"Detected {content_label.lower()} content and optimized "
                    f"{final_images_optimized} raster image"
                    f"{'s' if final_images_optimized != 1 else ''}; "
                    f"saved {saved_bytes:,} bytes."
                )
            else:
                message = (
                    f"Detected {content_label.lower()} content; saved "
                    f"{saved_bytes:,} bytes with structural compression."
                )

        _raise_if_cancelled(cancel_event)
        _emit(progress_callback, OptimizationStage.FINALIZING)
        _raise_if_cancelled(cancel_event)
        reserved_path = _reserve_output_path(
            source, directory, selected_options.suffix
        )
        _raise_if_cancelled(cancel_event)
        try:
            _atomic_replace(temporary_path, reserved_path)
        except OSError as exc:
            raise OutputWriteError(
                f"Could not finalize the output PDF '{reserved_path.name}'."
            ) from exc
        temporary_path = None
        output_path = reserved_path
        reserved_path = None

        result = OptimizationResult(
            source_path=source,
            output_path=output_path,
            status=status,
            input_size=input_size,
            output_size=output_size,
            saved_bytes=saved_bytes,
            saved_percent=(saved_bytes / input_size * 100.0) if input_size else 0.0,
            page_count=page_count,
            duration=time.perf_counter() - started_at,
            message=message,
            document_type=profile.document_type,
            images_optimized=final_images_optimized,
            compression_level=selected_options.compression_level,
        )
        _emit(progress_callback, OptimizationStage.COMPLETE)
        return result
    except _CancellationRequested:
        return _cancelled_result(
            source, input_size, page_count, started_at, progress_callback,
            selected_options.compression_level,
        )
    except pikepdf.PasswordError as exc:
        raise EncryptedPDFError(
            f"'{source.name}' is password protected and cannot be optimized."
        ) from exc
    except pikepdf.PdfError as exc:
        raise InvalidPDFError(
            f"'{source.name}' contains invalid PDF data and could not be optimized."
        ) from exc
    finally:
        # close() is idempotent and covers early signature/cancellation returns.
        pdf.close()
        _remove_if_present(temporary_path)
        _remove_if_present(reserved_path)


__all__ = [
    "CancellationEvent",
    "EncryptedPDFError",
    "InputPDFError",
    "InvalidPDFError",
    "OptimizationError",
    "OptimizationOptions",
    "OptimizationResult",
    "OptimizationStage",
    "OptimizationStatus",
    "OutputWriteError",
    "ProgressCallback",
    "SignedPDFError",
    "ValidationError",
    "next_available_output_path",
    "optimize_pdf",
]
