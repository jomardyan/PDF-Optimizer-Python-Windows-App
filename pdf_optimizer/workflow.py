"""Local preferences, reusable queues, and exportable optimization results."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .folder_optimizer import FolderOptimizationResult
from .optimizer import OptimizationResult

_MAX_JSON_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Preferences:
    compression_level: str = "medium"
    appearance: str = "System"
    output_dir: str | None = None


@dataclass(frozen=True)
class SavedQueue:
    paths: tuple[Path, ...]
    preferences: Preferences


def preferences_path() -> Path:
    override = os.environ.get("PDF_OPTIMIZER_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "preferences.json"
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return base / "PDFOptimizer" / "preferences.json"


def _read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        data = stream.read(_MAX_JSON_BYTES + 1)
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("This settings or queue file is too large.")
    return json.loads(data.decode("utf-8-sig"))


def _preferences(data: Any) -> Preferences:
    if not isinstance(data, dict):
        raise ValueError("Settings must be a JSON object.")
    level = data.get("compression_level", "medium")
    appearance = data.get("appearance", "System")
    output = data.get("output_dir")
    if level not in ("minimum", "medium", "strong"):
        raise ValueError("The saved compression level is not supported.")
    if appearance not in ("System", "Light", "Dark"):
        raise ValueError("The saved appearance is not supported.")
    if output is not None and (not isinstance(output, str) or not output or "\0" in output):
        raise ValueError("The saved output folder is invalid.")
    return Preferences(level, appearance, output)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Replace an app-owned settings file or explicitly chosen export atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="", dir=path.parent,
            prefix=".pdf_optimizer_settings_", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_preferences(path: Path) -> Preferences:
    try:
        return _preferences(_read_json(path))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return Preferences()


def save_preferences(path: Path, preferences: Preferences) -> None:
    data = asdict(preferences)
    _preferences(data)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def save_queue(path: Path, paths: list[Path], preferences: Preferences) -> None:
    _preferences(asdict(preferences))
    if len(paths) > 10_000:
        raise ValueError("The queue must contain at most 10,000 paths.")
    data = {
        "format": "pdf-optimizer-queue", "version": 1,
        "paths": [str(item.resolve()) for item in paths],
        "settings": asdict(preferences),
    }
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("The queue is too large to save.")
    atomic_write_text(path, content)


def load_queue(path: Path) -> SavedQueue:
    data = _read_json(path)
    if (
        not isinstance(data, dict) or data.get("format") != "pdf-optimizer-queue"
        or type(data.get("version")) is not int or data["version"] != 1
    ):
        raise ValueError("Choose a PDF Optimizer queue file (version 1).")
    paths = data.get("paths")
    if not isinstance(paths, list) or len(paths) > 10_000:
        raise ValueError("The queue must contain at most 10,000 paths.")
    resolved = []
    for value in paths:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("The queue contains an invalid source path.")
        source = Path(value).expanduser()
        resolved.append((path.parent / source).resolve())
    settings = _preferences(data.get("settings", {}))
    if settings.output_dir is not None:
        settings = Preferences(
            settings.compression_level, settings.appearance,
            str((path.parent / Path(settings.output_dir).expanduser()).resolve()),
        )
    return SavedQueue(tuple(resolved), settings)


@dataclass(frozen=True)
class ResultRecord:
    source: str
    kind: str
    status: str
    compression_level: str
    timestamp_utc: str
    output: str = ""
    input_bytes: int | None = None
    output_bytes: int | None = None
    saved_bytes: int | None = None
    saved_percent: float | None = None
    pages: int | None = None
    duration_seconds: float | None = None
    pdfs_copied_unchanged: int | None = None
    message: str = ""

    @classmethod
    def from_result(cls, result: OptimizationResult | FolderOptimizationResult) -> ResultRecord:
        is_folder = isinstance(result, FolderOptimizationResult)
        return cls(
            source=str(result.source_path), kind="folder" if is_folder else "pdf",
            status=result.status.value, compression_level=result.compression_level.value,
            timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            output=str(result.output_path) if result.output_path else "",
            input_bytes=result.input_size,
            output_bytes=result.output_size if result.output_path else None,
            saved_bytes=result.saved_bytes if result.output_path else None,
            saved_percent=round(result.saved_percent, 4) if result.output_path else None,
            pages=result.page_count, duration_seconds=round(result.duration, 3),
            pdfs_copied_unchanged=result.copied_pdf_count if is_folder else None,
            message=result.message,
        )

    @classmethod
    def incomplete(cls, path: Path, kind: str, status: str, level: str, message: str) -> ResultRecord:
        return cls(
            str(path), kind, status, level,
            datetime.now(timezone.utc).isoformat(timespec="seconds"), message=message,
        )


def _csv_cell(value: Any) -> Any:
    # Quotes alone do not prevent spreadsheet applications interpreting text
    # beginning with a formula prefix (including one after leading whitespace).
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_results(path: Path, records: list[ResultRecord]) -> None:
    if not records:
        raise ValueError("There are no results to export yet.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(ResultRecord.__dataclass_fields__))
    writer.writeheader()
    for record in records:
        writer.writerow({key: _csv_cell(value) for key, value in asdict(record).items()})
    atomic_write_text(path, output.getvalue(), encoding="utf-8-sig")
