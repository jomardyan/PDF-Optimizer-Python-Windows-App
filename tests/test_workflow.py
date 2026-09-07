from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pdf_optimizer import workflow


def test_preferences_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config" / "preferences.json"
    preferences = workflow.Preferences("strong", "Dark", str(tmp_path / "Exports"))
    workflow.save_preferences(path, preferences)
    assert workflow.load_preferences(path) == preferences


@pytest.mark.parametrize("content", ["broken json", "[]", '{"compression_level": false}', '{"appearance": "Unknown"}', '{"output_dir": 7}'])
def test_invalid_preferences_fall_back_to_defaults(tmp_path: Path, content: str) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(content)
    assert workflow.load_preferences(path) == workflow.Preferences()


def test_atomic_write_failure_preserves_previous_settings(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "preferences.json"
    original = workflow.Preferences()
    workflow.save_preferences(path, original)

    def fail(*args):
        raise PermissionError("File locked")

    monkeypatch.setattr(workflow.os, "replace", fail)
    with pytest.raises(PermissionError):
        workflow.save_preferences(path, workflow.Preferences("strong"))
    assert workflow.load_preferences(path) == original
    assert list(tmp_path.iterdir()) == [path]


def test_saved_queue_preserves_order_unicode_and_settings(tmp_path: Path) -> None:
    path = tmp_path / "saved queue.json"
    sources = [tmp_path / "Zoë report.pdf", tmp_path / "Project"]
    preferences = workflow.Preferences("minimum", "Light", str(tmp_path / "Exports"))
    workflow.save_queue(path, sources, preferences)
    loaded = workflow.load_queue(path)
    assert list(loaded.paths) == sources
    assert loaded.preferences == preferences


def test_relative_queue_paths_are_relative_to_manifest(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({
        "format": "pdf-optimizer-queue", "version": 1, "paths": ["input.pdf"],
        "settings": {"output_dir": "exports"},
    }))
    loaded = workflow.load_queue(path)
    assert loaded.paths == (tmp_path / "input.pdf",)
    assert loaded.preferences.output_dir == str(tmp_path / "exports")


@pytest.mark.parametrize("data", [[], {}, {"format": "pdf-optimizer-queue", "version": 2}, {"format": "pdf-optimizer-queue", "version": 1, "paths": [17]}])
def test_unsupported_queue_is_rejected(tmp_path: Path, data) -> None:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        workflow.load_queue(path)


def test_csv_includes_failed_cancelled_and_successful_results(tmp_path: Path) -> None:
    records = [
        workflow.ResultRecord(
            source="résumé.pdf", kind="pdf", status="optimized", compression_level="medium",
            timestamp_utc="2026-09-07T12:00:00+00:00", output="résumé_optimized.pdf",
            input_bytes=1000, output_bytes=700, saved_bytes=300, saved_percent=30.0,
        ),
        workflow.ResultRecord.incomplete(tmp_path / "broken.pdf", "pdf", "failed", "medium", "=DANGEROUS()"),
        workflow.ResultRecord.incomplete(tmp_path / "cancel.pdf", "pdf", "cancelled", "medium", "Not started"),
    ]
    path = tmp_path / "results.csv"
    workflow.export_results(path, records)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["status"] for row in rows] == ["optimized", "failed", "cancelled"]
    assert rows[0]["source"] == "résumé.pdf"
    assert rows[0]["saved_bytes"] == "300"
    assert rows[1]["output_bytes"] == ""
    assert rows[1]["message"] == "'=DANGEROUS()"


def test_empty_report_does_not_overwrite_file(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    path.write_text("keep")
    with pytest.raises(ValueError):
        workflow.export_results(path, [])
    assert path.read_text() == "keep"
