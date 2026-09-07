from __future__ import annotations

import subprocess
import sys
import time
import threading
from pathlib import Path
from tkinter import TclError

import pikepdf
import pytest

from pdf_optimizer import gui
from pdf_optimizer import workflow


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


def test_queue_polish_and_standard_actions(tmp_path: Path) -> None:
    source = tmp_path / "menu-actions.pdf"
    _create_reducible_pdf(source)
    app = _make_hidden_app()

    try:
        app._set_more_menu_states()
        assert app.more_menu.entrycget("Optimize queue", "state") == "disabled"
        assert app.drop_zone.winfo_manager() == "grid"
        assert app.file_list._parent_frame.winfo_manager() == ""
        assert app.empty_state.winfo_manager() == "grid"
        assert app.clear_button.winfo_manager() == ""
        app._apply_responsive_header(960)
        assert app.header_subtitle.cget("text") == "Smart optimization for PDFs, scans, and folders."
        app._apply_responsive_header(1180)
        assert app.header_subtitle.cget("text").startswith("Automatically adapt")

        app._add_paths([source])
        app._set_more_menu_states()

        assert app.drop_zone.winfo_manager() == ""
        assert app.file_list._parent_frame.winfo_manager() == "grid"
        assert app.empty_state.winfo_manager() == ""
        assert app.clear_button.winfo_manager() == "grid"
        assert app.queue_count.cget("text") == "1 item"
        assert app.more_menu.entrycget("Open selected location", "state") == "normal"
        assert app.more_menu.entrycget("Optimize queue", "state") == "normal"

        app._copy_selected_path()
        assert app.clipboard_get() == str(source.resolve())

        app._clear_files()
        assert app.drop_zone.winfo_manager() == "grid"
        assert app.file_list._parent_frame.winfo_manager() == ""
        assert app.empty_state.winfo_manager() == "grid"
        assert app.clear_button.winfo_manager() == ""
        assert app.queue_count.cget("text") == "0 items"
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


def test_cancel_output_picker_keeps_previous_selection(tmp_path: Path, monkeypatch) -> None:
    app = _make_hidden_app()
    choices = iter([str(tmp_path), ""])
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: next(choices))
    try:
        app._output_mode_changed("Choose folder")
        app._output_mode_changed("Choose folder")
        assert app.output_choice.get() == "Choose folder"
        assert app.selected_output_dir == tmp_path
        assert app.output_browse_button.winfo_manager() == "pack"
        app._output_mode_changed("Beside originals")
        assert app.selected_output_dir is None
        assert app.output_browse_button.winfo_manager() == ""
    finally:
        app.destroy()


def test_adding_folder_does_not_walk_tree_on_ui_thread(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Folder"
    source.mkdir()
    app = _make_hidden_app()

    def unexpected_walk(*args, **kwargs):
        raise AssertionError("Folder scanning must happen in the worker")

    monkeypatch.setattr(gui.os, "walk", unexpected_walk)
    try:
        app._add_paths([source])
        assert app.paths == [source]
    finally:
        app.destroy()


def test_close_keeps_polling_until_worker_finishes(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "cancel.pdf"
    _create_reducible_pdf(source)
    app = _make_hidden_app()
    release = threading.Event()
    real_optimize = gui.engine.optimize_pdf
    destroyed = []

    def delayed_optimize(*args, **kwargs):
        assert release.wait(5)
        return real_optimize(*args, **kwargs)

    monkeypatch.setattr(gui.engine, "optimize_pdf", delayed_optimize)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    actual_destroy = app.destroy
    monkeypatch.setattr(app, "destroy", lambda: destroyed.append(True))
    try:
        app._add_paths([source])
        app._start_batch()
        app._on_close()
        app.after(250, release.set)
        _wait_for_batch(app)
        assert destroyed
        assert not list(tmp_path.glob("*_optimized.pdf"))
    finally:
        release.set()
        if app.worker:
            app.worker.join(timeout=5)
        actual_destroy()


def test_minimum_layout_contains_controls_and_full_details(tmp_path: Path) -> None:
    app = _make_hidden_app()
    try:
        app.geometry("960x660")
        app.deiconify()
        for _ in range(5):
            app.update()
        for widget, parent in (
            (app.compression_switch, app.settings_card),
            (app.output_switch, app.settings_card),
            (app.browse_button, app.drop_zone),
        ):
            assert widget.winfo_x() >= 0
            assert widget.winfo_x() + widget.winfo_width() < parent.winfo_width()
            assert widget.winfo_y() + widget.winfo_height() < parent.winfo_height()
        assert app.empty_state.winfo_height() > 40

        source = tmp_path / ("very long document name " * 5 + ".pdf")
        _create_reducible_pdf(source)
        app._add_paths([source])
        for _ in range(3):
            app.update()
        row = app.rows[source]
        assert row.name_label.winfo_x() + row.name_label.winfo_width() <= row.status_label.winfo_x()
        assert row.detail_text
    finally:
        app.destroy()


def test_retry_only_processes_unfinished_items_and_exports_results(tmp_path: Path, monkeypatch) -> None:
    import csv

    good, broken = tmp_path / "good.pdf", tmp_path / "broken.pdf"
    _create_reducible_pdf(good)
    broken.write_text("not a PDF")
    app = _make_hidden_app()
    report = tmp_path / "results.csv"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kwargs: str(report))
    try:
        app._add_paths([good, broken])
        app._start_batch()
        _wait_for_batch(app)
        completed_output = app.rows[good].output_path
        assert app.records[broken].status == "failed"
        assert app.retry_button.winfo_manager() == "grid"
        app._export_results()
        with report.open(encoding="utf-8-sig", newline="") as stream:
            assert [row["status"] for row in csv.DictReader(stream)] == ["optimized", "failed"]

        _create_reducible_pdf(broken)
        app._retry_unfinished()
        _wait_for_batch(app)
        assert app.batch_total == 1
        assert app.rows[good].output_path == completed_output
        assert len(list(tmp_path.glob("good_optimized*.pdf"))) == 1
        assert app.records[broken].status == "optimized"
        assert app.retry_button.winfo_manager() == ""
        assert app.export_button.winfo_manager() == "grid"
    finally:
        app.destroy()


def test_save_load_reordered_queue_and_run_selected(tmp_path: Path, monkeypatch) -> None:
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    _create_reducible_pdf(first)
    _create_reducible_pdf(second)
    saved = tmp_path / "queue.json"
    app = _make_hidden_app()
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kwargs: str(saved))
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(saved))
    try:
        app._add_paths([first, second])
        app._move_selected(-1)
        assert app.paths == [second, first]
        app.compression_switch._select("Strong")
        app._save_queue()
        app._clear_files()
        app.compression_switch._select("Minimum")
        app._load_queue()
        assert app.paths == [second, first]
        assert app.compression_choice.get() == "Strong"
        assert not app.is_running
        app._select_file(second)
        app._optimize_selected()
        _wait_for_batch(app)
        assert app.rows[second].output_path
        assert app.rows[first].output_path is None
        assert list(app.records) == [second]
    finally:
        app.destroy()


def test_bad_queue_keeps_current_items(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "original.pdf"
    _create_reducible_pdf(source)
    saved = tmp_path / "invalid.json"
    saved.write_text('{"unexpected": true}')
    app = _make_hidden_app()
    errors = []
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(saved))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(args))
    try:
        app._add_paths([source])
        app._load_queue()
        assert errors
        assert app.paths == [source]
    finally:
        app.destroy()


def test_preferences_restore_on_start_and_reset(tmp_path: Path) -> None:
    expected = workflow.Preferences("strong", "Dark", str(tmp_path / "future exports"))
    workflow.save_preferences(workflow.preferences_path(), expected)
    app = _make_hidden_app()
    try:
        assert app.compression_switch.get() == "Strong"
        assert app.output_switch.get() == "Choose folder"
        assert app.selected_output_dir == tmp_path / "future exports"
        assert app.output_browse_button.winfo_manager() == "pack"
        app._reset_preferences()
        assert workflow.load_preferences(workflow.preferences_path()) == workflow.Preferences()
    finally:
        app.destroy()
