"""Content-aware raster image handling for PDF compression presets."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Any

import pikepdf
from PIL import Image


class CompressionLevel(str, Enum):
    """User-facing compression strengths."""

    MINIMUM = "minimum"
    MEDIUM = "medium"
    STRONG = "strong"


class DocumentType(str, Enum):
    """Broad PDF content category used by the automatic strategy."""

    TEXT_VECTOR = "text_vector"
    MIXED = "mixed"
    IMAGE_HEAVY = "image_heavy"


@dataclass(frozen=True, slots=True)
class DocumentProfile:
    """Content signals collected without changing a PDF."""

    document_type: DocumentType
    page_count: int
    image_count: int
    large_image_pages: int
    text_resource_pages: int
    encoded_image_bytes: int


@dataclass(frozen=True, slots=True)
class ImageOptimizationStats:
    """Summary of raster images changed by a lossy preset."""

    images_reencoded: int = 0
    images_skipped: int = 0
    encoded_bytes_before: int = 0
    encoded_bytes_after: int = 0


@dataclass(frozen=True, slots=True)
class _ImagePreset:
    jpeg_quality: int
    max_dimension: int
    minimum_pixels: int
    subsampling: int


_PRESETS = {
    CompressionLevel.MEDIUM: _ImagePreset(
        jpeg_quality=88,
        max_dimension=2600,
        minimum_pixels=700_000,
        subsampling=1,
    ),
    CompressionLevel.STRONG: _ImagePreset(
        jpeg_quality=76,
        max_dimension=1800,
        minimum_pixels=300_000,
        subsampling=2,
    ),
}


def coerce_compression_level(value: CompressionLevel | str) -> CompressionLevel:
    if isinstance(value, CompressionLevel):
        return value
    try:
        return CompressionLevel(str(value).lower())
    except ValueError as exc:
        choices = ", ".join(level.value for level in CompressionLevel)
        raise ValueError(f"Unknown compression level {value!r}; choose {choices}.") from exc


def _object_identity(obj: Any) -> tuple[str, int, int]:
    """Return a stable identity for direct or indirect pikepdf objects."""
    try:
        object_number, generation = obj.objgen
    except (AttributeError, TypeError, ValueError):
        object_number, generation = 0, 0
    if object_number:
        return ("indirect", int(object_number), int(generation))
    return ("direct", id(obj), 0)


def _walk_resource_images(
    resources: Any,
    visited_resources: set[tuple[str, int, int]],
) -> Iterator[Any]:
    if resources is None:
        return
    identity = _object_identity(resources)
    if identity in visited_resources:
        return
    visited_resources.add(identity)

    try:
        xobjects = resources.get("/XObject")
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return
    if xobjects is None:
        return

    try:
        values = tuple(xobjects.values())
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return
    for xobject in values:
        try:
            subtype = str(xobject.get("/Subtype"))
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            continue
        if subtype == "/Image":
            yield xobject
        elif subtype == "/Form":
            try:
                nested_resources = xobject.get("/Resources")
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                nested_resources = None
            yield from _walk_resource_images(nested_resources, visited_resources)


def _page_images(page: pikepdf.Page) -> tuple[Any, ...]:
    try:
        resources = page.obj.get("/Resources")
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return ()
    return tuple(_walk_resource_images(resources, set()))


def _resources_have_fonts(resources: Any, visited: set[tuple[str, int, int]]) -> bool:
    if resources is None:
        return False
    identity = _object_identity(resources)
    if identity in visited:
        return False
    visited.add(identity)
    try:
        fonts = resources.get("/Font")
        if fonts is not None and len(fonts) > 0:
            return True
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        forms = tuple(xobjects.values())
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return False
    for xobject in forms:
        try:
            if str(xobject.get("/Subtype")) != "/Form":
                continue
            if _resources_have_fonts(xobject.get("/Resources"), visited):
                return True
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            continue
    return False


def _image_dimensions(image: Any) -> tuple[int, int]:
    try:
        return int(image.get("/Width", 0)), int(image.get("/Height", 0))
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        return 0, 0


def _raw_image_size(image: Any) -> int:
    try:
        return len(image.read_raw_bytes())
    except (AttributeError, TypeError, ValueError, RuntimeError, pikepdf.PdfError):
        return 0


def analyze_document(
    pdf: pikepdf.Pdf,
    check_cancelled: Callable[[], None] | None = None,
) -> DocumentProfile:
    """Classify a PDF as text/vector, mixed, or image-heavy."""
    unique_images: dict[tuple[str, int, int], Any] = {}
    large_image_pages = 0
    text_resource_pages = 0

    for page in pdf.pages:
        if check_cancelled:
            check_cancelled()
        images = _page_images(page)
        page_has_large_image = False
        for image in images:
            unique_images.setdefault(_object_identity(image), image)
            width, height = _image_dimensions(image)
            if width >= 700 and height >= 700 and width * height >= 750_000:
                page_has_large_image = True
        if page_has_large_image:
            large_image_pages += 1
        try:
            resources = page.obj.get("/Resources")
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            resources = None
        if _resources_have_fonts(resources, set()):
            text_resource_pages += 1

    page_count = len(pdf.pages)
    image_count = len(unique_images)
    if image_count == 0:
        document_type = DocumentType.TEXT_VECTOR
    elif page_count and large_image_pages / page_count >= 0.6:
        # This includes OCR scans, which often have an invisible text layer.
        document_type = DocumentType.IMAGE_HEAVY
    elif text_resource_pages:
        document_type = DocumentType.MIXED
    else:
        document_type = DocumentType.IMAGE_HEAVY

    return DocumentProfile(
        document_type=document_type,
        page_count=page_count,
        image_count=image_count,
        large_image_pages=large_image_pages,
        text_resource_pages=text_resource_pages,
        encoded_image_bytes=sum(_raw_image_size(image) for image in unique_images.values()),
    )


def _unique_images(pdf: pikepdf.Pdf) -> tuple[Any, ...]:
    images: dict[tuple[str, int, int], Any] = {}
    for page in pdf.pages:
        for image in _page_images(page):
            images.setdefault(_object_identity(image), image)
    return tuple(images.values())


def _delete_key_if_present(stream: Any, key: str) -> None:
    try:
        if key in stream:
            del stream[key]
    except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
        pass


def optimize_raster_images(
    pdf: pikepdf.Pdf,
    profile: DocumentProfile,
    compression_level: CompressionLevel | str,
    check_cancelled: Callable[[], None] | None = None,
) -> ImageOptimizationStats:
    """Apply the selected image preset when the document profile warrants it.

    Text/vector-only documents stay on the lossless structural path at every
    level. Medium and Strong may re-encode sufficiently large RGB/grayscale
    raster images; transparent, masked, monochrome, and unusual color-space
    images are conservatively left untouched.
    """
    level = coerce_compression_level(compression_level)
    if level is CompressionLevel.MINIMUM or profile.document_type is DocumentType.TEXT_VECTOR:
        return ImageOptimizationStats()

    preset = _PRESETS[level]
    changed = 0
    skipped = 0
    bytes_before = 0
    bytes_after = 0

    for stream in _unique_images(pdf):
        if check_cancelled:
            check_cancelled()
        width, height = _image_dimensions(stream)
        pixels = width * height
        try:
            bits = int(stream.get("/BitsPerComponent", 8))
            has_mask = "/SMask" in stream or "/Mask" in stream
            is_mask = bool(stream.get("/ImageMask", False))
            has_custom_decode = "/Decode" in stream
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            skipped += 1
            continue
        if (
            width <= 0
            or height <= 0
            or pixels < preset.minimum_pixels
            or bits != 8
            or has_mask
            or is_mask
            or has_custom_decode
        ):
            skipped += 1
            continue

        original_size = _raw_image_size(stream)
        if original_size <= 0:
            skipped += 1
            continue

        try:
            pil_image = pikepdf.PdfImage(stream).as_pil_image()
        except (OSError, ValueError, RuntimeError, pikepdf.PdfError):
            skipped += 1
            continue

        working_image: Image.Image | None = None
        try:
            if pil_image.mode not in {"L", "RGB"}:
                skipped += 1
                continue
            longest = max(pil_image.size)
            if longest > preset.max_dimension:
                scale = preset.max_dimension / longest
                new_size = (
                    max(1, round(pil_image.width * scale)),
                    max(1, round(pil_image.height * scale)),
                )
                working_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)
            else:
                working_image = pil_image

            encoded = BytesIO()
            save_options: dict[str, Any] = {
                "format": "JPEG",
                "quality": preset.jpeg_quality,
                "optimize": True,
                "progressive": True,
            }
            if working_image.mode == "RGB":
                save_options["subsampling"] = preset.subsampling
            icc_profile = pil_image.info.get("icc_profile")
            if icc_profile:
                save_options["icc_profile"] = icc_profile
            working_image.save(encoded, **save_options)
            candidate = encoded.getvalue()
            if len(candidate) >= original_size * 0.97:
                skipped += 1
                continue

            stream.write(candidate, filter=pikepdf.Name.DCTDecode)
            stream["/Width"] = working_image.width
            stream["/Height"] = working_image.height
            stream["/BitsPerComponent"] = 8
            stream["/ColorSpace"] = (
                pikepdf.Name.DeviceGray
                if working_image.mode == "L"
                else pikepdf.Name.DeviceRGB
            )
            for key in (
                "/DecodeParms",
                "/F",
                "/FFilter",
                "/FDecodeParms",
                "/SMaskInData",
            ):
                _delete_key_if_present(stream, key)
            changed += 1
            bytes_before += original_size
            bytes_after += len(candidate)
        except (OSError, ValueError, RuntimeError, pikepdf.PdfError):
            skipped += 1
        finally:
            try:
                if working_image is not None and working_image is not pil_image:
                    working_image.close()
            finally:
                pil_image.close()

    return ImageOptimizationStats(
        images_reencoded=changed,
        images_skipped=skipped,
        encoded_bytes_before=bytes_before,
        encoded_bytes_after=bytes_after,
    )


def document_type_label(document_type: DocumentType) -> str:
    return {
        DocumentType.TEXT_VECTOR: "Text / vector",
        DocumentType.MIXED: "Mixed content",
        DocumentType.IMAGE_HEAVY: "Image-heavy / scanned",
    }[document_type]


__all__ = [
    "CompressionLevel",
    "DocumentProfile",
    "DocumentType",
    "ImageOptimizationStats",
    "analyze_document",
    "coerce_compression_level",
    "document_type_label",
    "optimize_raster_images",
]
