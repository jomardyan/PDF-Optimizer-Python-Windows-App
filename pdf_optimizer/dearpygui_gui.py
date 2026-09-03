"""Alternative Dear PyGui desktop interface for PDF Optimizer.

The optimizer and folder-cloning engines are shared with the CustomTkinter
application.  Dear PyGui mutations stay on the render thread; a worker thread
does the filesystem/PDF work and reports immutable events through a queue.
"""

# Dear PyGui's container stack is clearest as explicitly nested contexts.
# ruff: noqa: SIM117

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dearpygui.dearpygui as dpg

from . import folder_optimizer as folders
from . import optimizer as engine
from .smart_images import CompressionLevel, document_type_label

Color = tuple[int, int, int, int]
ItemTag = int | str


BG: Color = (10, 16, 29, 255)
SURFACE: Color = (17, 25, 40, 255)
SURFACE_ALT: Color = (23, 34, 53, 255)
BORDER: Color = (42, 57, 79, 255)
TEXT: Color = (244, 247, 252, 255)
MUTED: Color = (156, 171, 193, 255)
SUBTLE: Color = (113, 131, 157, 255)
BLUE: Color = (57, 128, 246, 255)
BLUE_HOVER: Color = (38, 105, 224, 255)
BLUE_SOFT: Color = (24, 48, 84, 255)
GREEN: Color = (74, 222, 128, 255)
GREEN_SOFT: Color = (18, 59, 44, 255)
AMBER: Color = (251, 191, 36, 255)
AMBER_SOFT: Color = (62, 47, 20, 255)
RED: Color = (251, 113, 133, 255)
RED_SOFT: Color = (68, 31, 42, 255)
SIDEBAR: Color = (16, 35, 63, 255)
SIDEBAR_CARD: Color = (25, 50, 82, 255)


def format_bytes(size: int) -> str:
    """Format a byte count for compact status text."""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _shorten(value: str, maximum: int = 58) -> str:
    if len(value) <= maximum:
        return value
    left = max(12, (maximum - 1) // 2)
    right = max(12, maximum - left - 1)
    return f"{value[:left]}…{value[-right:]}"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _open_folder(path: Path) -> None:
    folder = path if path.is_dir() else path.parent
    if sys.platform == "win32":
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


@dataclass(frozen=True, slots=True)
class QueuedItem:
    path: Path
    kind: str


@dataclass(frozen=True, slots=True)
class AddSummary:
    added: tuple[QueuedItem, ...]
    removed: tuple[QueuedItem, ...]
    rejected: int = 0
    duplicates: int = 0
    nested_ignored: int = 0


class QueueModel:
    """Ordered queue with canonical deduplication and parent-folder merging."""

    def __init__(self) -> None:
        self.items: list[QueuedItem] = []

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def add(self, raw_paths: Iterable[str | os.PathLike[str]]) -> AddSummary:
        added: list[QueuedItem] = []
        removed: list[QueuedItem] = []
        rejected = duplicates = nested_ignored = 0
        existing = {self._key(item.path) for item in self.items}

        for raw_path in raw_paths:
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                rejected += 1
                continue

            is_pdf = path.is_file() and path.suffix.lower() == ".pdf"
            is_folder = path.is_dir()
            if not is_pdf and not is_folder:
                rejected += 1
                continue

            key = self._key(path)
            if key in existing:
                duplicates += 1
                continue

            if any(
                item.kind == "folder" and _path_is_within(path, item.path)
                for item in self.items
            ):
                nested_ignored += 1
                continue

            if is_folder:
                descendants = [
                    item for item in self.items if _path_is_within(item.path, path)
                ]
                for descendant in descendants:
                    self.items.remove(descendant)
                    existing.discard(self._key(descendant.path))
                    removed.append(descendant)

            item = QueuedItem(path, "folder" if is_folder else "pdf")
            self.items.append(item)
            existing.add(key)
            added.append(item)

        return AddSummary(
            tuple(added),
            tuple(removed),
            rejected,
            duplicates,
            nested_ignored,
        )

    def remove(self, path: Path) -> bool:
        for item in self.items:
            if item.path == path:
                self.items.remove(item)
                return True
        return False

    def clear(self) -> None:
        self.items.clear()


@dataclass(slots=True)
class ItemPresentation:
    status: str = "Ready"
    tone: str = "blue"
    detail: str | None = None
    output_path: Path | None = None


@dataclass(slots=True)
class RowWidgets:
    container: ItemTag
    name: ItemTag
    detail: ItemTag
    status: ItemTag
    action: ItemTag


class DearPyGuiApp:
    """Controller and view builder for the Dear PyGui alternative."""

    PRIMARY = "dpg.primary"
    ROOT_TABLE = "dpg.root.table"
    SIDEBAR = "dpg.sidebar"
    SIDEBAR_FOOTER = "dpg.sidebar.footer"
    MAIN = "dpg.main"
    DROP_ZONE = "dpg.drop.zone"
    DROP_CONTENT = "dpg.drop.content"
    QUEUE_CARD = "dpg.queue.card"
    QUEUE_COUNT = "dpg.queue.count"
    QUEUE_LIST = "dpg.queue.list"
    EMPTY_STATE = "dpg.queue.empty"
    CLEAR_BUTTON = "dpg.queue.clear"
    SETTINGS_CARD = "dpg.settings.card"
    COMPRESSION = "dpg.settings.compression"
    COMPRESSION_DETAIL = "dpg.settings.compression.detail"
    OUTPUT_MODE = "dpg.settings.output.mode"
    OUTPUT_DETAIL = "dpg.settings.output.detail"
    ACTION_CARD = "dpg.action.card"
    STATUS_TITLE = "dpg.status.title"
    STATUS_DETAIL = "dpg.status.detail"
    PROGRESS = "dpg.status.progress"
    OPTIMIZE_BUTTON = "dpg.action.optimize"
    CANCEL_BUTTON = "dpg.action.cancel"
    CANCEL_PLACEHOLDER = "dpg.action.cancel.placeholder"
    PDF_DIALOG = "dpg.dialog.pdf"
    FOLDER_DIALOG = "dpg.dialog.folder"
    OUTPUT_DIALOG = "dpg.dialog.output"
    EXIT_DIALOG = "dpg.dialog.exit"
    MESSAGE_DIALOG = "dpg.dialog.message"

    def __init__(self) -> None:
        self.model = QueueModel()
        self.presentations: dict[Path, ItemPresentation] = {}
        self.rows: dict[Path, RowWidgets] = {}
        self.output_dir: Path | None = None
        self.is_running = False
        self.close_after_stop = False
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.batch_results: list[
            engine.OptimizationResult | folders.FolderOptimizationResult
        ] = []
        self.batch_errors = 0
        self.batch_skipped = 0
        self.fonts: dict[str, ItemTag] = {}
        self.viewport_ready = False
        self.layout_client_height = 0

    def build(self) -> None:
        self._build_fonts()
        self._build_themes()
        self._build_dialogs()
        self._build_primary_window()
        dpg.bind_theme("theme.global")
        self._rebuild_queue()

    def _build_fonts(self) -> None:
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        regular = fonts_dir / "segoeui.ttf"
        semibold = fonts_dir / "seguisb.ttf"
        if not regular.exists():
            return

        with dpg.font_registry():
            emphasis = semibold if semibold.exists() else regular
            self.fonts["small"] = dpg.add_font(str(regular), 13)
            self.fonts["body"] = dpg.add_font(str(regular), 15)
            self.fonts["medium"] = dpg.add_font(str(emphasis), 16)
            self.fonts["heading"] = dpg.add_font(str(emphasis), 23)
            self.fonts["title"] = dpg.add_font(str(emphasis), 27)
        dpg.bind_font(self.fonts["body"])

    def _font(self, item: ItemTag, name: str) -> None:
        if name in self.fonts:
            dpg.bind_item_font(item, self.fonts[name])

    def _build_themes(self) -> None:
        with dpg.theme(tag="theme.global"):
            with dpg.theme_component(dpg.mvAll):
                for target, color in (
                    (dpg.mvThemeCol_Text, TEXT),
                    (dpg.mvThemeCol_TextDisabled, SUBTLE),
                    (dpg.mvThemeCol_WindowBg, BG),
                    (dpg.mvThemeCol_ChildBg, SURFACE),
                    (dpg.mvThemeCol_PopupBg, SURFACE),
                    (dpg.mvThemeCol_Border, BORDER),
                    (dpg.mvThemeCol_FrameBg, SURFACE_ALT),
                    (dpg.mvThemeCol_FrameBgHovered, BLUE_SOFT),
                    (dpg.mvThemeCol_FrameBgActive, BLUE_SOFT),
                    (dpg.mvThemeCol_Button, SURFACE_ALT),
                    (dpg.mvThemeCol_ButtonHovered, BLUE_SOFT),
                    (dpg.mvThemeCol_ButtonActive, BLUE_SOFT),
                    (dpg.mvThemeCol_Header, BLUE_SOFT),
                    (dpg.mvThemeCol_HeaderHovered, BLUE_SOFT),
                    (dpg.mvThemeCol_HeaderActive, BLUE),
                    (dpg.mvThemeCol_CheckMark, BLUE),
                    (dpg.mvThemeCol_SliderGrab, BLUE),
                    (dpg.mvThemeCol_SliderGrabActive, BLUE_HOVER),
                    (dpg.mvThemeCol_Separator, BORDER),
                    (dpg.mvThemeCol_ScrollbarBg, BG),
                    (dpg.mvThemeCol_ScrollbarGrab, BORDER),
                    (dpg.mvThemeCol_ScrollbarGrabHovered, SUBTLE),
                ):
                    dpg.add_theme_color(target, color)
                for target, first, second in (
                    (dpg.mvStyleVar_WindowRounding, 0, 0),
                    (dpg.mvStyleVar_ChildRounding, 12, 0),
                    (dpg.mvStyleVar_FrameRounding, 8, 0),
                    (dpg.mvStyleVar_PopupRounding, 10, 0),
                    (dpg.mvStyleVar_ScrollbarRounding, 8, 0),
                    (dpg.mvStyleVar_GrabRounding, 8, 0),
                    (dpg.mvStyleVar_WindowBorderSize, 0, 0),
                    (dpg.mvStyleVar_ChildBorderSize, 1, 0),
                    (dpg.mvStyleVar_FramePadding, 10, 7),
                    (dpg.mvStyleVar_ItemSpacing, 9, 8),
                    (dpg.mvStyleVar_ScrollbarSize, 10, 0),
                ):
                    dpg.add_theme_style(target, first, second)

        self._solid_theme("theme.button.blue", BLUE, BLUE_HOVER, TEXT)
        self._outline_theme("theme.button.outline", SURFACE, BLUE_SOFT, BLUE)
        self._solid_theme("theme.button.subtle", SURFACE_ALT, BLUE_SOFT, MUTED)
        self._solid_theme("theme.button.red", RED_SOFT, (93, 39, 51, 255), RED)
        self._solid_theme("theme.badge.blue", BLUE_SOFT, BLUE_SOFT, BLUE)
        self._solid_theme("theme.badge.green", GREEN_SOFT, GREEN_SOFT, GREEN)
        self._solid_theme("theme.badge.amber", AMBER_SOFT, AMBER_SOFT, AMBER)
        self._solid_theme("theme.badge.red", RED_SOFT, RED_SOFT, RED)

        for tag, child_bg, border in (
            ("theme.sidebar", SIDEBAR, SIDEBAR),
            ("theme.sidebar.card", SIDEBAR_CARD, SIDEBAR_CARD),
            ("theme.card", SURFACE, BORDER),
            ("theme.row", SURFACE_ALT, BORDER),
        ):
            with dpg.theme(tag=tag):
                with dpg.theme_component(dpg.mvChildWindow):
                    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, child_bg)
                    dpg.add_theme_color(dpg.mvThemeCol_Border, border)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 12)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 12)

    @staticmethod
    def _solid_theme(tag: str, normal: Color, hovered: Color, text: Color) -> None:
        with dpg.theme(tag=tag):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, normal)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hovered)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, hovered)
                dpg.add_theme_color(dpg.mvThemeCol_Text, text)
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, text)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 9)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0)

    @staticmethod
    def _outline_theme(tag: str, normal: Color, hovered: Color, text: Color) -> None:
        with dpg.theme(tag=tag):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, normal)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hovered)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, hovered)
                dpg.add_theme_color(dpg.mvThemeCol_Border, text)
                dpg.add_theme_color(dpg.mvThemeCol_Text, text)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 9)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)

    def _build_dialogs(self) -> None:
        with dpg.file_dialog(
            tag=self.PDF_DIALOG,
            label="Choose PDF files",
            directory_selector=False,
            callback=self._pdfs_selected,
            file_count=0,
            modal=True,
            show=False,
            width=760,
            height=480,
        ):
            dpg.add_file_extension("PDF files (*.pdf){.pdf,.PDF}", color=BLUE)

        dpg.add_file_dialog(
            tag=self.FOLDER_DIALOG,
            label="Choose a folder to clone",
            directory_selector=True,
            callback=self._folder_selected,
            modal=True,
            show=False,
            width=760,
            height=480,
        )
        dpg.add_file_dialog(
            tag=self.OUTPUT_DIALOG,
            label="Choose an output folder",
            directory_selector=True,
            callback=self._output_selected,
            cancel_callback=self._output_cancelled,
            modal=True,
            show=False,
            width=760,
            height=480,
        )

        with dpg.window(
            tag=self.EXIT_DIALOG,
            label="Stop optimization?",
            modal=True,
            show=False,
            no_resize=True,
            no_move=True,
            width=460,
            height=180,
        ):
            dpg.add_text(
                "The current task will stop safely. Completed output files will be kept.",
                wrap=420,
                color=MUTED,
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                keep = dpg.add_button(
                    label="Keep working",
                    width=140,
                    callback=lambda: dpg.hide_item(self.EXIT_DIALOG),
                )
                stop = dpg.add_button(
                    label="Stop and exit",
                    width=150,
                    callback=self._confirm_exit,
                )
                dpg.bind_item_theme(keep, "theme.button.subtle")
                dpg.bind_item_theme(stop, "theme.button.red")

    def _build_primary_window(self) -> None:
        with dpg.window(
            tag=self.PRIMARY,
            label="PDF Optimizer",
            no_title_bar=True,
            no_move=True,
            no_resize=True,
            no_scrollbar=True,
            no_saved_settings=True,
        ):
            with dpg.table(
                tag=self.ROOT_TABLE,
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                borders_outerH=False,
                borders_outerV=False,
                policy=dpg.mvTable_SizingStretchProp,
                width=-1,
                height=-1,
            ):
                dpg.add_table_column(width_fixed=True, init_width_or_weight=270)
                dpg.add_table_column(width_stretch=True, init_width_or_weight=1)
                with dpg.table_row():
                    with dpg.child_window(
                        tag=self.SIDEBAR,
                        border=False,
                        no_scrollbar=True,
                        height=-1,
                    ):
                        self._build_sidebar()
                    with dpg.child_window(
                        tag=self.MAIN,
                        border=False,
                        no_scrollbar=False,
                        height=-1,
                    ):
                        self._build_main()
        dpg.bind_item_theme(self.SIDEBAR, "theme.sidebar")

    def _build_sidebar(self) -> None:
        dpg.add_spacer(height=12)
        logo = dpg.add_button(label="P", width=48, height=48)
        dpg.bind_item_theme(logo, "theme.button.blue")
        self._font(logo, "title")
        dpg.add_spacer(height=4)
        title = dpg.add_text("PDF Optimizer", color=TEXT)
        self._font(title, "heading")
        dpg.add_text("Dear PyGui edition", color=(113, 212, 178, 255))
        dpg.add_text("Smaller files. Same visuals.", color=MUTED)
        dpg.add_spacer(height=22)

        with dpg.child_window(height=112, border=False) as smart_card:
            label = dpg.add_text("SMART AUTO MODE", color=(113, 225, 179, 255))
            self._font(label, "small")
            dpg.add_spacer(height=4)
            dpg.add_text(
                "Text and vectors stay crisp.\nLarge images follow your chosen level.",
                color=TEXT,
            )
        dpg.bind_item_theme(smart_card, "theme.sidebar.card")

        dpg.add_spacer(height=26)
        for line in (
            "•   Original files stay untouched",
            "•   Folder structure is preserved",
            "•   Processing stays on this PC",
        ):
            dpg.add_text(line, color=(134, 230, 190, 255))
            dpg.add_spacer(height=8)

        with dpg.group(tag=self.SIDEBAR_FOOTER):
            dpg.add_text(
                "Minimum is fully lossless.\nMedium is recommended for everyday use.",
                color=MUTED,
            )

    def _build_main(self) -> None:
        self._build_header()
        dpg.add_spacer(height=10)
        self._build_drop_zone()
        dpg.add_spacer(height=10)
        self._build_queue_card()
        dpg.add_spacer(height=10)
        self._build_settings()
        dpg.add_spacer(height=10)
        self._build_action_card()

    def _build_header(self) -> None:
        with dpg.table(
            header_row=False,
            borders_innerH=False,
            borders_innerV=False,
            height=66,
            width=-1,
            policy=dpg.mvTable_SizingStretchProp,
        ):
            dpg.add_table_column(width_stretch=True, init_width_or_weight=1)
            dpg.add_table_column(width_fixed=True, init_width_or_weight=292)
            with dpg.table_row():
                with dpg.group():
                    title = dpg.add_text("Compress without compromise", color=TEXT)
                    self._font(title, "heading")
                    dpg.add_text(
                        "Automatic optimization for text PDFs, scans, images, and folders.",
                        color=MUTED,
                    )
                with dpg.group(horizontal=True):
                    add_folder = dpg.add_button(
                        tag="control.add.folder",
                        label="+  Add folder",
                        width=132,
                        height=40,
                        callback=self._show_folder_dialog,
                    )
                    add_pdfs = dpg.add_button(
                        tag="control.add.pdfs",
                        label="+  Add PDFs",
                        width=132,
                        height=40,
                        callback=self._show_pdf_dialog,
                    )
                    dpg.bind_item_theme(add_folder, "theme.button.outline")
                    dpg.bind_item_theme(add_pdfs, "theme.button.blue")

    def _build_drop_zone(self) -> None:
        with dpg.child_window(
            tag=self.DROP_ZONE,
            height=126,
            width=-1,
            border=True,
            no_scrollbar=True,
        ):
            with dpg.group(tag=self.DROP_CONTENT):
                heading = dpg.add_text("Choose PDF files or a complete folder", color=TEXT)
                self._font(heading, "medium")
                dpg.add_text(
                    "Folder clones preserve every subfolder and non-PDF file.",
                    color=MUTED,
                )
                with dpg.group(horizontal=True):
                    browse = dpg.add_button(
                        label="Browse PDFs",
                        width=118,
                        callback=self._show_pdf_dialog,
                    )
                    folder = dpg.add_button(
                        label="Add folder",
                        width=110,
                        callback=self._show_folder_dialog,
                    )
                    dpg.bind_item_theme(browse, "theme.button.outline")
                    dpg.bind_item_theme(folder, "theme.button.subtle")
        dpg.bind_item_theme(self.DROP_ZONE, "theme.card")

    def _build_queue_card(self) -> None:
        with dpg.child_window(
            tag=self.QUEUE_CARD,
            height=250,
            width=-1,
            border=True,
            no_scrollbar=True,
        ):
            with dpg.table(
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                height=35,
                width=-1,
            ):
                dpg.add_table_column(width_stretch=True)
                dpg.add_table_column(width_fixed=True, init_width_or_weight=90)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        heading = dpg.add_text("Queue", color=TEXT)
                        self._font(heading, "medium")
                        count = dpg.add_button(
                            tag=self.QUEUE_COUNT,
                            label="0 items",
                            width=66,
                            height=26,
                            enabled=False,
                        )
                        dpg.bind_item_theme(count, "theme.badge.blue")
                    clear = dpg.add_button(
                        tag=self.CLEAR_BUTTON,
                        label="Clear queue",
                        width=88,
                        show=False,
                        callback=self._clear_queue,
                    )
                    dpg.bind_item_theme(clear, "theme.button.subtle")

            with dpg.child_window(
                tag=self.QUEUE_LIST,
                height=-1,
                width=-1,
                border=False,
            ):
                pass
        dpg.bind_item_theme(self.QUEUE_CARD, "theme.card")

    def _build_settings(self) -> None:
        with dpg.child_window(
            tag=self.SETTINGS_CARD,
            height=166,
            width=-1,
            border=True,
            no_scrollbar=True,
        ):
            with dpg.table(
                header_row=False,
                borders_innerH=True,
                borders_innerV=False,
                borders_outerH=False,
                height=-1,
                width=-1,
                policy=dpg.mvTable_SizingStretchProp,
            ):
                dpg.add_table_column(width_stretch=True, init_width_or_weight=1)
                dpg.add_table_column(width_fixed=True, init_width_or_weight=330)
                with dpg.table_row():
                    with dpg.group():
                        label = dpg.add_text("Compression level", color=TEXT)
                        self._font(label, "medium")
                        dpg.add_text(
                            tag=self.COMPRESSION_DETAIL,
                            default_value="Recommended • high-quality images; text stays crisp",
                            color=MUTED,
                        )
                    dpg.add_radio_button(
                        tag=self.COMPRESSION,
                        items=("Minimum", "Medium", "Strong"),
                        default_value="Medium",
                        horizontal=True,
                        callback=self._compression_changed,
                    )
                with dpg.table_row():
                    with dpg.group():
                        label = dpg.add_text("Output location", color=TEXT)
                        self._font(label, "medium")
                        dpg.add_text(
                            tag=self.OUTPUT_DETAIL,
                            default_value="Create collision-safe _optimized outputs beside each source",
                            color=MUTED,
                        )
                    dpg.add_radio_button(
                        tag=self.OUTPUT_MODE,
                        items=("Beside originals", "Choose folder"),
                        default_value="Beside originals",
                        horizontal=True,
                        callback=self._output_mode_changed,
                    )
        dpg.bind_item_theme(self.SETTINGS_CARD, "theme.card")

    def _build_action_card(self) -> None:
        with dpg.child_window(
            tag=self.ACTION_CARD,
            height=104,
            width=-1,
            border=True,
            no_scrollbar=True,
        ):
            with dpg.table(
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                height=-1,
                width=-1,
                policy=dpg.mvTable_SizingStretchProp,
            ):
                dpg.add_table_column(width_stretch=True, init_width_or_weight=1)
                dpg.add_table_column(width_fixed=True, init_width_or_weight=320)
                with dpg.table_row():
                    with dpg.group():
                        title = dpg.add_text(
                            tag=self.STATUS_TITLE,
                            default_value="Ready to optimize",
                            color=TEXT,
                        )
                        self._font(title, "medium")
                        dpg.add_text(
                            tag=self.STATUS_DETAIL,
                            default_value="Add PDF files or a folder to begin",
                            color=MUTED,
                        )
                        dpg.add_progress_bar(
                            tag=self.PROGRESS,
                            default_value=0,
                            width=-1,
                            height=5,
                            show=False,
                        )
                    with dpg.group():
                        dpg.add_spacer(height=15)
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(
                                tag=self.CANCEL_PLACEHOLDER,
                                width=108,
                                height=43,
                            )
                            cancel = dpg.add_button(
                                tag=self.CANCEL_BUTTON,
                                label="Cancel",
                                width=108,
                                height=43,
                                show=False,
                                callback=self._cancel_batch,
                            )
                            optimize = dpg.add_button(
                                tag=self.OPTIMIZE_BUTTON,
                                label="Optimize",
                                width=180,
                                height=43,
                                enabled=False,
                                callback=self._start_batch,
                            )
                            dpg.bind_item_theme(cancel, "theme.button.red")
                            dpg.bind_item_theme(optimize, "theme.button.blue")
        dpg.bind_item_theme(self.ACTION_CARD, "theme.card")

    def _show_pdf_dialog(self, *_args: Any) -> None:
        if not self.is_running:
            dpg.show_item(self.PDF_DIALOG)

    def _show_folder_dialog(self, *_args: Any) -> None:
        if not self.is_running:
            dpg.show_item(self.FOLDER_DIALOG)

    @staticmethod
    def _dialog_paths(app_data: Any) -> list[Path]:
        if not isinstance(app_data, dict):
            return []
        selections = app_data.get("selections")
        if isinstance(selections, dict) and selections:
            return [Path(value) for value in selections.values()]
        selected = app_data.get("file_path_name")
        return [Path(selected)] if selected else []

    def _pdfs_selected(self, _sender: Any, app_data: Any, _user_data: Any = None) -> None:
        self._add_paths(self._dialog_paths(app_data))

    def _folder_selected(self, _sender: Any, app_data: Any, _user_data: Any = None) -> None:
        self._add_paths(self._dialog_paths(app_data)[:1])

    def _add_paths(self, paths: Iterable[Path]) -> None:
        if self.is_running:
            return
        summary = self.model.add(paths)
        for item in summary.removed:
            self.presentations.pop(item.path, None)
        for item in summary.added:
            self.presentations[item.path] = ItemPresentation(
                detail=self._ready_detail(item)
            )
        self._rebuild_queue()

        if summary.added:
            count = len(self.model.items)
            self._set_status(
                f"{count} item{'s' if count != 1 else ''} ready",
                "Sources untouched • folder structure preserved",
            )
            dpg.hide_item(self.PROGRESS)
        ignored = summary.rejected + summary.duplicates + summary.nested_ignored
        if ignored and not summary.added:
            self._set_status(
                "Nothing new was added",
                f"Ignored {ignored} invalid, duplicate, or already-covered item"
                f"{'s' if ignored != 1 else ''}",
            )

    @staticmethod
    def _ready_detail(item: QueuedItem) -> str:
        if item.kind == "folder":
            return "Complete folder clone • PDFs optimized; every other file preserved"
        try:
            return f"{format_bytes(item.path.stat().st_size)} • {_shorten(str(item.path.parent), 62)}"
        except OSError:
            return _shorten(str(item.path.parent), 62)

    def _rebuild_queue(self) -> None:
        if not dpg.does_item_exist(self.QUEUE_LIST):
            return
        dpg.delete_item(self.QUEUE_LIST, children_only=True)
        self.rows.clear()

        if not self.model.items:
            with dpg.group(tag=self.EMPTY_STATE, parent=self.QUEUE_LIST):
                empty = dpg.add_text(
                    "No items yet — add PDFs or a folder above",
                    color=SUBTLE,
                )
                self._font(empty, "body")
        else:
            for item in self.model.items:
                self._build_queue_row(item)

        count = len(self.model.items)
        dpg.configure_item(
            self.QUEUE_COUNT,
            label=f"{count} item{'s' if count != 1 else ''}",
        )
        dpg.configure_item(
            self.CLEAR_BUTTON,
            show=bool(count),
            enabled=bool(count and not self.is_running),
        )
        dpg.configure_item(self.DROP_ZONE, show=not bool(count))
        self._update_optimize_button()
        self.resize()

    def _build_queue_row(self, item: QueuedItem) -> None:
        presentation = self.presentations.setdefault(
            item.path, ItemPresentation(detail=self._ready_detail(item))
        )
        container = dpg.add_child_window(
            parent=self.QUEUE_LIST,
            height=76,
            width=-1,
            border=True,
            no_scrollbar=True,
        )
        dpg.bind_item_theme(container, "theme.row")
        with dpg.table(
            parent=container,
            header_row=False,
            borders_innerH=False,
            borders_innerV=False,
            height=-1,
            width=-1,
            policy=dpg.mvTable_SizingStretchProp,
        ):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=54)
            dpg.add_table_column(width_stretch=True, init_width_or_weight=1)
            dpg.add_table_column(width_fixed=True, init_width_or_weight=126)
            dpg.add_table_column(width_fixed=True, init_width_or_weight=74)
            with dpg.table_row():
                icon = dpg.add_button(
                    label="DIR" if item.kind == "folder" else "PDF",
                    width=46,
                    height=45,
                )
                dpg.bind_item_theme(
                    icon,
                    "theme.badge.green" if item.kind == "folder" else "theme.badge.blue",
                )
                with dpg.group():
                    name = dpg.add_text(_shorten(item.path.name, 62), color=TEXT)
                    self._font(name, "medium")
                    detail = dpg.add_text(
                        presentation.detail or self._ready_detail(item),
                        color=MUTED,
                    )
                status = dpg.add_button(
                    label=presentation.status,
                    width=118,
                    height=31,
                )
                dpg.bind_item_theme(status, f"theme.badge.{presentation.tone}")
                action_label = "Open" if presentation.output_path else "Remove"
                action = dpg.add_button(
                    label=action_label,
                    width=68,
                    height=31,
                    enabled=not self.is_running,
                    callback=(
                        self._open_output if presentation.output_path else self._remove_item
                    ),
                    user_data=presentation.output_path or item.path,
                )
                dpg.bind_item_theme(action, "theme.button.subtle")
        self.rows[item.path] = RowWidgets(container, name, detail, status, action)

    def _remove_item(self, _sender: Any, _app_data: Any, user_data: Any) -> None:
        if self.is_running:
            return
        path = Path(user_data)
        if self.model.remove(path):
            self.presentations.pop(path, None)
            self._rebuild_queue()
        if not self.model.items:
            self._set_status("Ready to optimize", "Add PDF files or a folder to begin")
            dpg.hide_item(self.PROGRESS)

    def _clear_queue(self, *_args: Any) -> None:
        if self.is_running:
            return
        self.model.clear()
        self.presentations.clear()
        self._rebuild_queue()
        self._set_status("Ready to optimize", "Add PDF files or a folder to begin")
        dpg.hide_item(self.PROGRESS)

    def _open_output(self, _sender: Any, _app_data: Any, user_data: Any) -> None:
        try:
            _open_folder(Path(user_data))
        except OSError as exc:
            self._show_message("Could not open output", str(exc))

    def _compression_changed(self, _sender: Any, app_data: Any, _user_data: Any = None) -> None:
        descriptions = {
            "Minimum": "Fully lossless • structural compression only",
            "Medium": "Recommended • high-quality images; text stays crisp",
            "Strong": "Smallest scans • image softness may be visible when zoomed",
        }
        dpg.set_value(self.COMPRESSION_DETAIL, descriptions.get(str(app_data), ""))

    def _output_mode_changed(self, _sender: Any, app_data: Any, _user_data: Any = None) -> None:
        if str(app_data) == "Choose folder":
            dpg.show_item(self.OUTPUT_DIALOG)
        else:
            self.output_dir = None
            dpg.set_value(
                self.OUTPUT_DETAIL,
                "Create collision-safe _optimized outputs beside each source",
            )

    def _output_selected(self, _sender: Any, app_data: Any, _user_data: Any = None) -> None:
        paths = self._dialog_paths(app_data)
        if not paths:
            self._output_cancelled()
            return
        try:
            self.output_dir = paths[0].resolve(strict=True)
        except (OSError, RuntimeError):
            self.output_dir = None
            self._output_cancelled()
            return
        dpg.set_value(self.OUTPUT_MODE, "Choose folder")
        dpg.set_value(self.OUTPUT_DETAIL, _shorten(str(self.output_dir), 72))

    def _output_cancelled(self, *_args: Any) -> None:
        if self.output_dir is None:
            dpg.set_value(self.OUTPUT_MODE, "Beside originals")
            dpg.set_value(
                self.OUTPUT_DETAIL,
                "Create collision-safe _optimized outputs beside each source",
            )

    def _start_batch(self, *_args: Any) -> None:
        if self.is_running or not self.model.items:
            return
        output_mode = dpg.get_value(self.OUTPUT_MODE)
        if output_mode == "Choose folder" and self.output_dir is None:
            dpg.show_item(self.OUTPUT_DIALOG)
            return
        output_dir = self.output_dir if output_mode == "Choose folder" else None
        if output_dir:
            unsafe = next(
                (
                    item.path
                    for item in self.model.items
                    if item.kind == "folder" and _path_is_within(output_dir, item.path)
                ),
                None,
            )
            if unsafe:
                self._show_message(
                    "Choose a different output folder",
                    "A clone cannot be created inside its source tree.\n\n"
                    f"Choose a location outside:\n{unsafe}",
                )
                return

        self.is_running = True
        self.close_after_stop = False
        self.cancel_event.clear()
        self.batch_results.clear()
        self.batch_errors = 0
        self.batch_skipped = 0
        for item in self.model.items:
            self.presentations[item.path] = ItemPresentation(
                status="Ready", detail=self._ready_detail(item)
            )
        self._rebuild_queue()
        self._set_running_controls(True)

        selected_level = str(dpg.get_value(self.COMPRESSION)).lower()
        level = CompressionLevel(selected_level)
        options = engine.OptimizationOptions(compression_level=level)
        self._set_status(
            "Preparing smart optimization…",
            f"Item 1 of {len(self.model.items)} • {level.value.title()} compression",
        )
        dpg.set_value(self.PROGRESS, -1.0)
        dpg.configure_item(self.PROGRESS, overlay="Preparing…", show=True)

        snapshot = list(self.model.items)
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(snapshot, output_dir, options),
            name="pdf-optimizer-dearpygui-worker",
            daemon=True,
        )
        self.worker.start()

    def _run_batch(
        self,
        items: list[QueuedItem],
        output_dir: Path | None,
        options: engine.OptimizationOptions,
    ) -> None:
        total = len(items)
        for index, item in enumerate(items, start=1):
            if self.cancel_event.is_set():
                self.events.put(("canceled_remaining", items[index - 1 :]))
                break
            self.events.put(("item_start", item, index, total))

            def report_stage(
                stage: engine.OptimizationStage,
                current_path: Path = item.path,
            ) -> None:
                self.events.put(("stage", current_path, stage))

            def report_folder_progress(
                progress: folders.FolderProgress,
                current_path: Path = item.path,
            ) -> None:
                self.events.put(("folder_progress", current_path, progress))

            try:
                if item.kind == "folder":
                    result = folders.clone_and_optimize_folder(
                        item.path,
                        output_parent=output_dir,
                        options=options,
                        cancel_event=self.cancel_event,
                        progress_callback=report_folder_progress,
                    )
                else:
                    result = engine.optimize_pdf(
                        item.path,
                        output_dir=output_dir,
                        options=options,
                        cancel_event=self.cancel_event,
                        progress_callback=report_stage,
                    )
                self.events.put(("result", item.path, result, index, total))
            except Exception as exc:  # noqa: BLE001 - isolate individual queue items
                self.events.put(("error", item.path, exc, index, total))

            if self.cancel_event.is_set():
                self.events.put(("canceled_remaining", items[index:]))
                break
        self.events.put(("batch_done", self.cancel_event.is_set()))

    def drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "item_start":
                    _, item, index, total = event
                    action = "Cloning" if item.kind == "folder" else "Optimizing"
                    self._set_status(
                        f"{action} {_shorten(item.path.name, 44)}",
                        f"Item {index} of {total} • Checking content",
                    )
                    self._set_row_status(
                        item.path,
                        "Scanning" if item.kind == "folder" else "Checking",
                        "blue",
                    )
                    dpg.set_value(self.PROGRESS, (index - 1) / total)
                    dpg.configure_item(self.PROGRESS, overlay=f"{index} of {total}")
                elif kind == "stage":
                    self._show_stage(event[1], event[2])
                elif kind == "folder_progress":
                    self._show_folder_progress(event[1], event[2])
                elif kind == "result":
                    _, path, result, index, total = event
                    self.batch_results.append(result)
                    self._show_result(path, result)
                    value = getattr(result.status, "value", str(result.status)).lower()
                    if "signed" in value or "cancel" in value:
                        self.batch_skipped += 1
                    dpg.set_value(self.PROGRESS, index / total)
                elif kind == "error":
                    _, path, exc, index, total = event
                    class_name = type(exc).__name__.lower()
                    skipped = "encrypted" in class_name or "signed" in class_name
                    self._set_row_status(
                        path,
                        "Skipped" if skipped else "Failed",
                        "amber" if skipped else "red",
                        str(exc),
                    )
                    if skipped:
                        self.batch_skipped += 1
                    else:
                        self.batch_errors += 1
                    dpg.set_value(self.PROGRESS, index / total)
                elif kind == "canceled_remaining":
                    for item in event[1]:
                        self._set_row_status(item.path, "Canceled", "amber", "Not started")
                elif kind == "batch_done":
                    self._finish_batch(bool(event[1]))
        except queue.Empty:
            return

    def _set_row_status(
        self,
        path: Path,
        status: str,
        tone: str,
        detail: str | None = None,
        output_path: Path | None = None,
    ) -> None:
        presentation = self.presentations.setdefault(path, ItemPresentation())
        presentation.status = status
        presentation.tone = tone
        if detail is not None:
            presentation.detail = detail
        if output_path is not None:
            presentation.output_path = output_path
        row = self.rows.get(path)
        if row is None:
            return
        dpg.configure_item(row.status, label=status)
        dpg.bind_item_theme(row.status, f"theme.badge.{tone}")
        if detail is not None:
            dpg.set_value(row.detail, detail)
        if presentation.output_path:
            dpg.configure_item(
                row.action,
                label="Open",
                callback=self._open_output,
                user_data=presentation.output_path,
                enabled=not self.is_running,
            )

    def _show_stage(self, path: Path, stage: Any) -> None:
        value = getattr(stage, "value", str(stage)).lower()
        mapping = {
            "checking": ("Checking", "Checking document", "blue"),
            "optimizing": ("Optimizing", "Compressing PDF structure and eligible images", "blue"),
            "verifying": ("Verifying", "Validating the optimized copy", "blue"),
            "finalizing": ("Saving", "Safely writing the output", "blue"),
            "signed_skipped": ("Signed · skipped", "Digital signatures remain untouched", "amber"),
            "cancelled": ("Canceled", "Stopped safely", "amber"),
        }
        if value in mapping:
            label, detail, tone = mapping[value]
            self._set_row_status(path, label, tone, detail)
            dpg.set_value(self.STATUS_DETAIL, detail)

    def _show_folder_progress(
        self,
        path: Path,
        progress: folders.FolderProgress,
    ) -> None:
        current = _shorten(str(progress.current_path), 54) if progress.current_path else ""
        count = (
            f"{min(progress.completed_files + 1, progress.total_files)} of {progress.total_files}"
            if progress.total_files
            else ""
        )
        if progress.stage is folders.FolderStage.SCANNING:
            label, detail, tone = "Scanning", "Reading the complete folder structure", "blue"
        elif progress.stage is folders.FolderStage.COPYING:
            label, detail, tone = "Cloning", f"Copying {current} • {count}", "blue"
        elif progress.stage is folders.FolderStage.OPTIMIZING_PDF:
            label, detail, tone = "Optimizing PDFs", f"{current} • {count}", "blue"
        elif progress.stage is folders.FolderStage.FINALIZING:
            label, detail, tone = "Finalizing", "Publishing the completed clone safely", "blue"
        elif progress.stage is folders.FolderStage.CANCELLED:
            label, detail, tone = "Canceled", "No partial clone was kept", "amber"
        else:
            return
        self._set_row_status(path, label, tone, detail)
        dpg.set_value(self.STATUS_DETAIL, detail)

    def _show_result(
        self,
        path: Path,
        result: engine.OptimizationResult | folders.FolderOptimizationResult,
    ) -> None:
        value = getattr(result.status, "value", str(result.status)).lower()
        if isinstance(result, folders.FolderOptimizationResult) and result.output_path:
            detail = (
                f"{result.pdf_total} PDF{'s' if result.pdf_total != 1 else ''} • "
                f"{result.other_files_copied} other file"
                f"{'s' if result.other_files_copied != 1 else ''} • "
                f"Saved {format_bytes(result.saved_bytes)}"
            )
            self._set_row_status(
                path, "Folder cloned", "green", detail, Path(result.output_path)
            )
        elif value == "optimized":
            content = document_type_label(result.document_type)
            image_note = (
                f" • {result.images_optimized} image"
                f"{'s' if result.images_optimized != 1 else ''} optimized"
                if result.images_optimized
                else ""
            )
            detail = (
                f"{format_bytes(result.input_size)} → {format_bytes(result.output_size)} • "
                f"{result.saved_percent:.1f}% smaller • {content}{image_note}"
            )
            self._set_row_status(
                path,
                "Optimized",
                "green",
                detail,
                Path(result.output_path) if result.output_path else None,
            )
        elif "signed" in value:
            self._set_row_status(path, "Signed · skipped", "amber", result.message)
        elif "cancel" in value:
            self._set_row_status(path, "Canceled", "amber", result.message)
        else:
            content = document_type_label(result.document_type)
            detail = f"{format_bytes(result.input_size)} • Already efficient • {content}"
            self._set_row_status(
                path,
                "Already optimal",
                "amber",
                detail,
                Path(result.output_path) if result.output_path else None,
            )

    def _finish_batch(self, was_canceled: bool) -> None:
        self.is_running = False
        self._set_running_controls(False)
        successful = [result for result in self.batch_results if result.output_path]
        saved = sum(max(0, int(result.saved_bytes)) for result in successful)
        input_total = sum(max(0, int(result.input_size)) for result in successful)
        percent = (saved / input_total * 100) if input_total else 0.0
        optimized = sum(
            result.optimized_count
            if isinstance(result, folders.FolderOptimizationResult)
            else 1
            for result in self.batch_results
            if getattr(result.status, "value", str(result.status)).lower() == "optimized"
        )
        folder_clones = sum(
            isinstance(result, folders.FolderOptimizationResult) for result in successful
        )
        images = sum(result.images_optimized for result in successful)

        if was_canceled:
            self._set_status(
                "Optimization canceled safely",
                f"Completed outputs were kept • Saved {format_bytes(saved)}",
            )
            dpg.configure_item(self.PROGRESS, overlay="Canceled")
        elif self.batch_errors:
            self._set_status(
                f"Finished with {self.batch_errors} error"
                f"{'s' if self.batch_errors != 1 else ''}",
                f"{len(successful)} completed • {self.batch_skipped} skipped • "
                f"Saved {format_bytes(saved)}",
            )
            dpg.configure_item(self.PROGRESS, overlay="Finished with errors")
        else:
            detail = f"{len(successful)} item{'s' if len(successful) != 1 else ''} completed"
            if folder_clones:
                detail += f" • {folder_clones} folder{'s' if folder_clones != 1 else ''} cloned"
            detail += f" • {optimized} PDF{'s' if optimized != 1 else ''} reduced"
            if images:
                detail += f" • {images} image{'s' if images != 1 else ''} optimized"
            detail += f" • Saved {format_bytes(saved)} ({percent:.1f}%)"
            if self.batch_skipped:
                detail += f" • {self.batch_skipped} skipped"
            self._set_status("Optimization complete", detail)
            dpg.set_value(self.PROGRESS, 1.0)
            dpg.configure_item(self.PROGRESS, overlay="Complete")

        for path, row in self.rows.items():
            presentation = self.presentations[path]
            dpg.configure_item(
                row.action,
                enabled=True,
                label="Open" if presentation.output_path else "Remove",
                callback=self._open_output if presentation.output_path else self._remove_item,
                user_data=presentation.output_path or path,
            )
        if self.close_after_stop:
            dpg.stop_dearpygui()

    def _set_running_controls(self, running: bool) -> None:
        for tag in (
            "control.add.folder",
            "control.add.pdfs",
            self.CLEAR_BUTTON,
            self.COMPRESSION,
            self.OUTPUT_MODE,
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=not running)
        for row in self.rows.values():
            dpg.configure_item(row.action, enabled=not running)
        dpg.configure_item(self.CANCEL_BUTTON, show=running, enabled=running, label="Cancel")
        dpg.configure_item(self.CANCEL_PLACEHOLDER, show=not running)
        self._update_optimize_button()

    def _update_optimize_button(self) -> None:
        if not dpg.does_item_exist(self.OPTIMIZE_BUTTON):
            return
        count = len(self.model.items)
        dpg.configure_item(
            self.OPTIMIZE_BUTTON,
            label=(
                f"Optimize {count} item{'s' if count != 1 else ''}" if count else "Optimize"
            ),
            enabled=bool(count and not self.is_running),
        )

    def _cancel_batch(self, *_args: Any) -> None:
        if not self.is_running or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        dpg.configure_item(self.CANCEL_BUTTON, enabled=False, label="Stopping…")
        self._set_status(
            "Stopping safely…",
            "Temporary PDF and folder-clone data will be removed",
        )

    def _set_status(self, title: str, detail: str) -> None:
        dpg.set_value(self.STATUS_TITLE, title)
        dpg.set_value(self.STATUS_DETAIL, detail)

    def _show_message(self, title: str, message: str) -> None:
        if dpg.does_item_exist(self.MESSAGE_DIALOG):
            dpg.delete_item(self.MESSAGE_DIALOG)
        with dpg.window(
            tag=self.MESSAGE_DIALOG,
            label=title,
            modal=True,
            no_resize=True,
            width=500,
            height=210,
        ):
            dpg.add_text(message, wrap=455, color=MUTED)
            dpg.add_spacer(height=12)
            close = dpg.add_button(
                label="OK",
                width=100,
                callback=lambda: dpg.delete_item(self.MESSAGE_DIALOG),
            )
            dpg.bind_item_theme(close, "theme.button.blue")

    def request_exit(self, *_args: Any) -> None:
        if not self.is_running:
            dpg.stop_dearpygui()
            return
        dpg.show_item(self.EXIT_DIALOG)

    def _confirm_exit(self, *_args: Any) -> None:
        dpg.hide_item(self.EXIT_DIALOG)
        self.close_after_stop = True
        self._cancel_batch()

    def resize(
        self,
        _sender: Any = None,
        app_data: Any = None,
        _user_data: Any = None,
    ) -> None:
        if not self.viewport_ready or not dpg.does_item_exist(self.QUEUE_CARD):
            return
        if isinstance(app_data, (list, tuple)) and len(app_data) >= 4:
            height = max(1, int(app_data[3]))
        else:
            height = max(1, dpg.get_viewport_client_height())
        self.layout_client_height = height
        drop_height = 136 if not self.model.items else 0
        queue_height = max(132, height - 104 - drop_height - 176 - 96 - 91)
        dpg.configure_item(self.QUEUE_CARD, height=queue_height)

    @staticmethod
    def _center_item(item: ItemTag, parent: ItemTag) -> None:
        if not dpg.does_item_exist(item) or not dpg.is_item_shown(item):
            return
        parent_width, parent_height = dpg.get_item_rect_size(parent)
        item_width, item_height = dpg.get_item_rect_size(item)
        if parent_width <= 1 or item_width <= 1:
            return
        centered_y = max(8, round((parent_height - item_height) / 2))
        maximum_y = max(8, round(parent_height - item_height - 12))
        dpg.set_item_pos(
            item,
            [
                max(8, round((parent_width - item_width) / 2)),
                min(centered_y, maximum_y),
            ],
        )

    def after_render(self) -> None:
        """Apply centering after Dear PyGui has measured dynamic content."""
        client_height = dpg.get_viewport_client_height()
        if client_height > 0 and client_height != self.layout_client_height:
            self.resize()
        self._center_item(self.DROP_CONTENT, self.DROP_ZONE)
        self._center_item(self.EMPTY_STATE, self.QUEUE_LIST)

        if not dpg.does_item_exist(self.SIDEBAR_FOOTER):
            return
        _, sidebar_height = dpg.get_item_rect_size(self.SIDEBAR)
        _, footer_height = dpg.get_item_rect_size(self.SIDEBAR_FOOTER)
        if sidebar_height > 1 and footer_height > 1:
            dpg.set_item_pos(
                self.SIDEBAR_FOOTER,
                [8, max(540, round(sidebar_height - footer_height - 26))],
            )


def run_app(smoke_test: bool = False) -> int:
    """Build and run the alternative interface; return a process exit code."""
    dpg.create_context()
    try:
        dpg.configure_app(manual_callback_management=True)
        controller = DearPyGuiApp()
        controller.build()
        dpg.create_viewport(
            title="PDF Optimizer - Dear PyGui",
            width=1180,
            height=800,
            min_width=980,
            min_height=800,
            clear_color=BG,
            disable_close=True,
            vsync=True,
        )
        dpg.setup_dearpygui()
        dpg.set_primary_window(controller.PRIMARY, True)
        dpg.set_viewport_resize_callback(controller.resize)
        dpg.set_exit_callback(controller.request_exit)
        dpg.show_viewport()
        controller.viewport_ready = True
        controller.resize()

        if smoke_test:
            for _ in range(3):
                jobs = dpg.get_callback_queue()
                if jobs:
                    dpg.run_callbacks(jobs)
                controller.drain_events()
                dpg.render_dearpygui_frame()
                controller.after_render()
            print("Dear PyGui smoke test passed")
            return 0

        while dpg.is_dearpygui_running():
            jobs = dpg.get_callback_queue()
            if jobs:
                dpg.run_callbacks(jobs)
            controller.drain_events()
            dpg.render_dearpygui_frame()
            controller.after_render()
        return 0
    finally:
        dpg.destroy_context()


__all__ = ["AddSummary", "DearPyGuiApp", "QueueModel", "QueuedItem", "run_app"]
