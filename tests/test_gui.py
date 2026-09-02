from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from tkinter import TclError

import pikepdf
import pytest

from pdf_optimizer import gui


def _create_reducible_pdf(path: Path) -> None:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 300))
        page.obj["/Contents"] = pdf.make_stream(
            b"0 0 m 100 100 l S\n" * 10_000
        )
        pdf.save(
            path,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )


def _make_hidden_app() -> gui.PDFOptimizerApp:
    try:
        app = gui.PDFOptimizerApp()
    except TclError as exc:
        pytest.skip(f"Tk display is unavailable: {exc}")
    app.withdraw()
    return app


def _wait_for_batch(app: gui.PDFOptimizerApp) -> None:
    deadline = time.monotonic() + 15
    while app.is_running and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    app.update()
    assert not app.is_running


def test_gui_runs_pdf_and_folder_background_batches(tmp_path: Path) -> None:
    source = tmp_path / "gui-integration.pdf"
    _create_reducible_pdf(source)
    app = _make_hidden_app()

    try:
        app._add_paths([source])
        app._start_batch()
        _wait_for_batch(app)

        row = app.rows[source.resolve()]
        assert row.output_path is not None
        assert row.output_path.exists()
        assert row.status_label.cget("text") in {"Optimized", "Already optimal"}
        assert app.status_title.cget("text") == "Optimization complete"

        app._clear_files()
        folder_source = tmp_path / "Project"
        folder_source.mkdir()
        _create_reducible_pdf(folder_source / "document.pdf")
        (folder_source / "model.xlsx").write_bytes(b"spreadsheet-data")
        app._add_paths([folder_source])
        assert app.item_kinds[folder_source.resolve()] == "folder"
        app._start_batch()
        _wait_for_batch(app)

        folder_row = app.rows[folder_source.resolve()]
        assert folder_row.status_label.cget("text") == "Folder cloned"
        assert folder_row.output_path == tmp_path / "Project_optimized"
        assert (folder_row.output_path / "document.pdf").exists()
        assert (folder_row.output_path / "model.xlsx").read_bytes() == b"spreadsheet-data"
    finally:
        try:
            app.destroy()
        except TclError:
            pass


def test_native_drag_drop_failure_falls_back_to_picker() -> None:
    if not gui._HAS_DND_PACKAGE:
        pytest.skip("TkinterDnD2 is not installed")

    script = """
import pdf_optimizer.gui as gui

def fail_to_load(_root):
    raise RuntimeError("simulated unsupported native library")

gui.TkinterDnD.require = fail_to_load
app = gui.PDFOptimizerApp()
app.withdraw()
assert app._dnd_available is False
assert "Add folder" in app.drop_zone.winfo_children()[1].cget("text")
app.destroy()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
