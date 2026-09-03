from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pikepdf
import pytest

dpg = pytest.importorskip("dearpygui.dearpygui")

from pdf_optimizer.dearpygui_gui import DearPyGuiApp, QueueModel, format_bytes


def _create_reducible_pdf(path: Path) -> None:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 300))
        page.obj["/Contents"] = pdf.make_stream(b"0 0 m 100 100 l S\n" * 10_000)
        pdf.save(
            path,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )


def test_queue_model_deduplicates_and_merges_parent_folder(tmp_path: Path) -> None:
    project = tmp_path / "Project"
    nested = project / "reports"
    nested.mkdir(parents=True)
    pdf = nested / "summary.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    model = QueueModel()
    first = model.add([pdf])
    duplicate = model.add([pdf])
    parent = model.add([project])
    covered = model.add([nested])

    assert [item.path for item in first.added] == [pdf.resolve()]
    assert duplicate.duplicates == 1
    assert parent.removed == first.added
    assert [(item.path, item.kind) for item in model.items] == [
        (project.resolve(), "folder")
    ]
    assert covered.nested_ignored == 1


def test_queue_model_rejects_unsupported_and_missing_paths(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not a PDF", encoding="utf-8")

    model = QueueModel()
    summary = model.add([text_file, tmp_path / "missing.pdf"])

    assert summary.rejected == 2
    assert not model.items


def test_queue_model_remove_and_clear(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.PDF"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")

    model = QueueModel()
    model.add([first, second])

    assert model.remove(first.resolve()) is True
    assert model.remove(first.resolve()) is False
    assert [item.path for item in model.items] == [second.resolve()]

    model.clear()
    assert not model.items


def test_controller_runs_pdf_optimization_batch(tmp_path: Path) -> None:
    source = tmp_path / "dearpygui-integration.pdf"
    _create_reducible_pdf(source)

    dpg.create_context()
    controller = DearPyGuiApp()
    try:
        dpg.configure_app(manual_callback_management=True)
        controller.build()
        assert dpg.does_item_exist("control.add.folder")
        assert dpg.does_item_exist("control.add.pdfs")
        assert dpg.is_item_shown(controller.CANCEL_PLACEHOLDER)
        assert not dpg.is_item_shown(controller.CANCEL_BUTTON)
        controller._add_paths([source])
        controller._start_batch()
        assert not dpg.is_item_enabled("control.add.folder")
        assert not dpg.is_item_enabled("control.add.pdfs")
        assert not dpg.is_item_shown(controller.CANCEL_PLACEHOLDER)
        assert dpg.is_item_shown(controller.CANCEL_BUTTON)

        deadline = time.monotonic() + 15
        while controller.is_running and time.monotonic() < deadline:
            controller.drain_events()
            time.sleep(0.01)
        controller.drain_events()

        presentation = controller.presentations[source.resolve()]
        assert not controller.is_running
        assert presentation.output_path is not None
        assert presentation.output_path.exists()
        assert presentation.status in {"Optimized", "Already optimal"}
        assert dpg.get_value(controller.STATUS_TITLE) == "Optimization complete"
        assert dpg.is_item_enabled("control.add.folder")
        assert dpg.is_item_enabled("control.add.pdfs")
        assert dpg.is_item_shown(controller.CANCEL_PLACEHOLDER)
        assert not dpg.is_item_shown(controller.CANCEL_BUTTON)
    finally:
        controller.cancel_event.set()
        if controller.worker is not None:
            controller.worker.join(timeout=2)
        dpg.destroy_context()


def test_minimum_viewport_has_no_overflow_and_centers_empty_state() -> None:
    script = """
import dearpygui.dearpygui as dpg

from pdf_optimizer.dearpygui_gui import DearPyGuiApp

dpg.create_context()
controller = DearPyGuiApp()
try:
    dpg.configure_app(manual_callback_management=True)
    controller.build()
    dpg.create_viewport(
        title="PDF Optimizer layout test",
        width=980,
        height=800,
        min_width=980,
        min_height=800,
    )
    dpg.setup_dearpygui()
    dpg.set_primary_window(controller.PRIMARY, True)
    dpg.show_viewport()
    controller.viewport_ready = True
    controller.resize()
    for _ in range(4):
        dpg.render_dearpygui_frame()
        controller.after_render()

    assert dpg.get_y_scroll_max(controller.MAIN) == 0
    assert dpg.get_y_scroll_max(controller.QUEUE_CARD) == 0

    for item, parent in (
        (controller.DROP_CONTENT, controller.DROP_ZONE),
        (controller.EMPTY_STATE, controller.QUEUE_LIST),
    ):
        item_x = dpg.get_item_pos(item)[0]
        item_width = dpg.get_item_rect_size(item)[0]
        parent_width = dpg.get_item_rect_size(parent)[0]
        assert abs((item_x + item_width / 2) - parent_width / 2) <= 1
finally:
    dpg.destroy_context()
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


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (1024**2, "1.0 MB")],
)
def test_format_bytes(size: int, expected: str) -> None:
    assert format_bytes(size) == expected
