"""Smaller page content and Flate streams without changing how pages look.

Many PDFs (browser "print to PDF", converted slides, text turned into outlines)
carry megabytes of vector drawing commands written with six decimals
(``0.160000 1095.520020 l``), split over dozens of tiny streams that each
compress poorly. Rewriting that data is the biggest saving available for them:

* all levels: page content streams are merged and numbers lose redundant
  trailing zeros (identical values, shorter text);
* Medium/Strong: path coordinates are truncated to 3/2 decimals (an error far
  below a hundredth of a millimetre on a page);
* all levels: Flate streams are recompressed at the maximum zlib level.

Only pages whose content is plain drawing operators are touched. Anything
containing strings, inline images, comments or dictionaries is left exactly as
it was, so text, fonts and images are never altered.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import pikepdf

from .smart_images import CompressionLevel, coerce_compression_level

# Pages with less drawing data than this gain too little to be worth rewriting.
_MINIMUM_CONTENT_BYTES = 4096
_MINIMUM_FLATE_BYTES = 512

_UNSAFE_MARKERS = (b"(", b")", b"<", b">", b"%")
_UNSAFE_OPERATOR = re.compile(rb"(?<![^\s])(?:BI|ID|EI)(?![^\s])")
_NUMBER = rb"-?\d*\.?\d+"
# a b c d e f cm
_MATRIX = re.compile(
    rb"(?<![^\s])("
    + rb")[ \t]+(".join([_NUMBER] * 4)
    + rb")[ \t]+"
    + _NUMBER
    + rb"[ \t]+"
    + _NUMBER
    + rb"[ \t]+cm(?![^\s])"
)
# Trailing zeros of plain decimals ("0.480000" -> "0.48", "1.000000" -> "1").
_TRAILING_ZEROS = re.compile(rb"(?<![^\s\[])([+-]?\d+)\.(\d*?)0*(?=[\s\]]|\Z)")
_DANGLING_POINT = re.compile(rb"(?<![^\s\[])([+-]?\d+)\.(?=[\s\]]|\Z)")
# Numbers on one line followed by more numbers and a path operator are page
# coordinates (m l c v y re) and safe to shorten.
_PATH_TAIL = rb"(?=(?:[ \t]+-?[\d.]+)*[ \t]+(?:m|l|c|v|y|re)(?![^\s]))"
# Only the translation (e f) of a ``cm`` is a coordinate.
_MATRIX_TRANSLATION = rb"(?=(?:[ \t]+-?[\d.]+)?[ \t]+cm(?![^\s]))"
_DECIMALS = {CompressionLevel.MEDIUM: 3, CompressionLevel.STRONG: 2}


@dataclass(frozen=True, slots=True)
class ContentStreamStats:
    pages_rewritten: int = 0
    streams_recompressed: int = 0


def _rewrite_vector_content(data: bytes, decimals: int | None) -> bytes | None:
    """Return a shorter equivalent of pure-drawing content, or None if unsafe.

    With ``decimals`` set, coordinates are truncated to that many decimals
    (an error under 0.01 units at 2); otherwise only redundant zeros are
    dropped and every value stays exactly the same.
    """
    # Cheap substring checks first: they run at memchr speed on large streams.
    if any(marker in data for marker in _UNSAFE_MARKERS):
        return None
    if any(word in data for word in (b"BI", b"ID", b"EI")) and _UNSAFE_OPERATOR.search(data):
        return None

    if decimals is not None:
        # Truncation assumes the page space is never magnified by a transform.
        for matrix in set(_MATRIX.findall(data)):
            if any(abs(float(value)) > 1.0001 for value in matrix):
                decimals = None
                break

    if decimals is not None:
        excess = rb"(?<![^\s])(-?\d+\.\d{%d})\d+" % decimals
        data = re.sub(excess + _PATH_TAIL, rb"\g<1>", data)
        data = re.sub(excess + _MATRIX_TRANSLATION, rb"\g<1>", data)
    data = _TRAILING_ZEROS.sub(rb"\g<1>.\g<2>", data)
    return _DANGLING_POINT.sub(rb"\g<1>", data)


def _stream_filters(stream: pikepdf.Stream) -> list[str]:
    value = stream.get("/Filter")
    if value is None:
        return []
    if isinstance(value, pikepdf.Array):
        return [str(item) for item in value]
    return [str(value)]


def _content_streams(page: pikepdf.Page) -> list[pikepdf.Stream] | None:
    contents = page.obj.get("/Contents")
    if contents is None:
        return None
    items = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    if not items or not all(isinstance(item, pikepdf.Stream) for item in items):
        return None
    return items


def _optimize_page_contents(
    pdf: pikepdf.Pdf,
    level: CompressionLevel,
    check_cancelled: Callable[[], None] | None,
    handled: set[tuple[int, int]],
) -> int:
    decimals = _DECIMALS.get(level)
    rewritten: dict[tuple[tuple[int, int], ...], pikepdf.Stream | None] = {}
    pages = 0
    for page in pdf.pages:
        if check_cancelled:
            check_cancelled()
        streams = _content_streams(page)
        if streams is None:
            continue
        key = tuple(stream.objgen for stream in streams)
        if (0, 0) in key:
            continue
        if key not in rewritten:
            replacement: pikepdf.Stream | None = None
            try:
                data = b"\n".join(stream.read_bytes() for stream in streams)
                if len(data) >= _MINIMUM_CONTENT_BYTES:
                    shortened = _rewrite_vector_content(data, decimals)
                    if shortened is not None:
                        original_size = sum(
                            len(stream.read_raw_bytes()) for stream in streams
                        )
                        compressed = zlib.compress(shortened, 9)
                        if len(compressed) < original_size:
                            replacement = pikepdf.Stream(pdf, b"")
                            replacement.write(compressed, filter=pikepdf.Name.FlateDecode)
                            handled.add(replacement.objgen)
                            handled.update(key)
            except (pikepdf.PdfError, ValueError, OverflowError, zlib.error):
                replacement = None
            rewritten[key] = replacement
        replacement = rewritten[key]
        if replacement is not None:
            page.obj["/Contents"] = replacement
            pages += 1
    return pages


def _recompress_flate_streams(
    pdf: pikepdf.Pdf,
    check_cancelled: Callable[[], None] | None,
    skip: set[tuple[int, int]],
) -> int:
    """Recompress plain Flate streams at zlib level 9 (qpdf stops at level 6)."""
    changed = 0
    for obj in list(pdf.objects):
        if not isinstance(obj, pikepdf.Stream):
            continue
        if check_cancelled:
            check_cancelled()
        if obj.objgen in skip:
            continue
        try:
            if _stream_filters(obj) != ["/FlateDecode"] or "/DecodeParms" in obj:
                continue
            raw = obj.read_raw_bytes()
            if len(raw) < _MINIMUM_FLATE_BYTES:
                continue
            compressed = zlib.compress(obj.read_bytes(), 9)
            if len(compressed) < len(raw):
                obj.write(compressed, filter=pikepdf.Name.FlateDecode)
                changed += 1
        except (pikepdf.PdfError, ValueError, RuntimeError, zlib.error):
            continue
    return changed


def optimize_content_streams(
    pdf: pikepdf.Pdf,
    compression_level: CompressionLevel | str,
    recompress_flate: bool = True,
    check_cancelled: Callable[[], None] | None = None,
) -> ContentStreamStats:
    """Shrink page drawing data and Flate streams in place (see module docs)."""
    level = coerce_compression_level(compression_level)
    # Streams just written, and the old content they replace, need no further work.
    handled: set[tuple[int, int]] = set()
    pages = _optimize_page_contents(pdf, level, check_cancelled, handled)
    recompressed = (
        _recompress_flate_streams(pdf, check_cancelled, handled) if recompress_flate else 0
    )
    return ContentStreamStats(pages_rewritten=pages, streams_recompressed=recompressed)


__all__ = ["ContentStreamStats", "optimize_content_streams"]
