from __future__ import annotations

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


def test_gui_runs_a_complete_background_batch(tmp_path: Path) -> None:
    source = tmp_path / "gui-integration.pdf"
    _create_reducible_pdf(source)
    app = _make_hidden_app()

    try:
        app._add_paths([source])
        app._start_batch()
        deadline = time.monotonic() + 15
        while app.is_running and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()

        row = app.rows[source.resolve()]
        assert not app.is_running
        assert row.output_path is not None
        assert row.output_path.exists()
        assert row.status_label.cget("text") in {"Optimized", "Already optimal"}
        assert app.status_title.cget("text") == "Optimization complete"
    finally:
        try:
            app.destroy()
        except TclError:
            pass


def test_native_drag_drop_failure_falls_back_to_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not gui._HAS_DND_PACKAGE:
        pytest.skip("TkinterDnD2 is not installed")

    def fail_to_load(_root: object) -> None:
        raise RuntimeError("simulated unsupported native library")

    monkeypatch.setattr(gui.TkinterDnD, "require", fail_to_load)
    app = _make_hidden_app()
    try:
        assert app._dnd_available is False
        assert "Browse" in app.drop_zone.winfo_children()[1].cget("text")
    finally:
        app.destroy()
