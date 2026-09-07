"""Modern batch desktop interface for smart PDF and folder optimization."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from tkinter import Menu, TclError, filedialog, messagebox
from typing import Any

import customtkinter as ctk

from . import folder_optimizer as folders
from . import optimizer as engine
from . import workflow
from .smart_images import CompressionLevel, document_type_label

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND_PACKAGE = True
except ImportError:  # The file picker remains fully functional without drag/drop.
    DND_FILES = None
    TkinterDnD = None
    _HAS_DND_PACKAGE = False


ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


COLORS = {
    "background": ("#F3F6FA", "#0B1120"),
    "surface": ("#FFFFFF", "#111827"),
    "surface_alt": ("#F8FAFD", "#172033"),
    "border": ("#DDE5EF", "#273449"),
    "text": ("#162238", "#F5F7FB"),
    "muted": ("#65748B", "#9BA8BA"),
    "subtle": ("#8B98AA", "#78869A"),
    "blue": ("#2563EB", "#3B82F6"),
    "blue_hover": ("#1D4ED8", "#2563EB"),
    "blue_soft": ("#EAF1FF", "#172A4A"),
    "button": ("#E8EEF5", "#1C2A40"),
    "button_hover": ("#DCE5F0", "#2A3B54"),
    "choice": ("#DCE8FD", "#28466F"),
    "choice_text": ("#17489E", "#DCEAFF"),
    "green": ("#167A52", "#4ADE80"),
    "green_soft": ("#E7F7F0", "#14382C"),
    "amber": ("#A15C08", "#FBBF24"),
    "amber_soft": ("#FFF4DD", "#3C2D13"),
    "red": ("#B4232C", "#FB7185"),
    "red_soft": ("#FDECEE", "#42202A"),
    "sidebar": "#10233F",
    "sidebar_card": "#193252",
    "sidebar_text": "#F7FAFF",
    "sidebar_muted": "#AFC0D6",
}

FONT_FAMILY = "Segoe UI"


class ChoiceControl(ctk.CTkFrame):
    """Padded choices with independent buttons, avoiding joined-corner seams."""

    def __init__(
        self, master: Any, *, values: list[str], variable: ctk.StringVar,
        command: Callable[[str], None],
    ) -> None:
        super().__init__(
            master, width=300, height=38, corner_radius=8,
            fg_color=COLORS["surface_alt"], border_width=0,
        )
        self.grid_propagate(False)
        self.grid_rowconfigure(0, weight=1)
        self.variable = variable
        self.command = command
        self.buttons: dict[str, ctk.CTkButton] = {}
        for index, value in enumerate(values):
            self.grid_columnconfigure(index, weight=1, uniform="choice")
            button = ctk.CTkButton(
                self, text=value, width=0, height=30, corner_radius=6,
                border_width=0, font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
                text_color_disabled=COLORS["subtle"],
                command=lambda choice=value: self._select(choice),
            )
            button.grid(
                row=0, column=index, sticky="nsew", pady=4,
                padx=(4 if index == 0 else 2, 4 if index == len(values) - 1 else 2),
            )
            self.buttons[value] = button
        self._trace = variable.trace_add("write", self._refresh)
        self._refresh()

    def get(self) -> str:
        return self.variable.get()

    def set(self, value: str) -> None:
        self.variable.set(value)

    def _select(self, value: str) -> None:
        self.set(value)
        self.command(value)

    def _refresh(self, *_args: Any) -> None:
        for value, button in self.buttons.items():
            selected = value == self.get()
            button.configure(
                fg_color=COLORS["choice"] if selected else COLORS["surface_alt"],
                hover_color=COLORS["choice"] if selected else COLORS["button_hover"],
                text_color=COLORS["choice_text"] if selected else COLORS["muted"],
            )

    def configure(self, **kwargs: Any) -> None:
        state = kwargs.pop("state", None)
        if state is not None:
            for button in self.buttons.values():
                button.configure(state=state)
        super().configure(**kwargs)

    def destroy(self) -> None:
        self.variable.trace_remove("write", self._trace)
        super().destroy()


def format_bytes(size: int) -> str:
    """Format a byte count for compact UI display."""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _shorten(value: str, maximum: int = 52) -> str:
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
    _open_document(folder)


def _open_document(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"The file or folder no longer exists: {path}")
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


if _HAS_DND_PACKAGE:

    class _BaseWindow(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        """CustomTkinter root with optional drag/drop methods mixed in."""

else:

    class _BaseWindow(ctk.CTk):
        """CustomTkinter root used when drag/drop is not installed."""


class FileRow(ctk.CTkFrame):
    """A queued PDF or folder clone and its current/result state."""

    def __init__(
        self,
        master: Any,
        path: Path,
        on_remove: Callable[[Path], None],
        on_select: Callable[[Path], None],
        kind: str = "pdf",
    ) -> None:
        super().__init__(
            master,
            height=76,
            corner_radius=12,
            fg_color=COLORS["surface_alt"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self.path = path
        self.kind = kind
        self.output_path: Path | None = None
        self.detail_text = ""
        self._locked = False
        self._on_remove = on_remove
        self._on_select = on_select
        self.grid_propagate(False)
        self.grid_columnconfigure(1, weight=1)

        self.icon = ctk.CTkLabel(
            self,
            width=42,
            height=46,
            corner_radius=9,
            fg_color=COLORS["green_soft"] if kind == "folder" else COLORS["blue_soft"],
            text="DIR" if kind == "folder" else "PDF",
            text_color=COLORS["green"] if kind == "folder" else COLORS["blue"],
            font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
        )
        self.icon.grid(row=0, column=0, rowspan=2, padx=(14, 12), pady=14)

        self.name_label = ctk.CTkLabel(
            self,
            text=_shorten(path.name),
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 14, "bold"),
        )
        self.name_label.grid(row=0, column=1, sticky="sew", pady=(13, 1))

        detail = self._ready_detail()
        self.detail_label = ctk.CTkLabel(
            self,
            text=detail,
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
        )
        self.detail_label.grid(row=1, column=1, sticky="new", pady=(0, 12))

        self.status_label = ctk.CTkLabel(
            self,
            width=112,
            height=28,
            corner_radius=14,
            text="Ready",
            fg_color=COLORS["blue_soft"],
            text_color=COLORS["blue"],
            font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
        )
        self.status_label.grid(row=0, column=2, rowspan=2, padx=(12, 8), pady=22)

        self.action_button = ctk.CTkButton(
            self,
            width=70,
            height=30,
            corner_radius=8,
            text="Remove",
            fg_color="transparent",
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
            command=lambda: self._on_remove(self.path),
        )
        self.action_button.grid(row=0, column=3, rowspan=2, padx=(0, 10), pady=22)

        for widget in (self, self.icon, self.name_label, self.detail_label, self.status_label):
            widget.bind("<Button-1>", lambda _event: self._on_select(self.path))
            widget.bind("<Double-Button-1>", lambda _event: self.show_details())
        self.detail_text = detail
        self.bind("<Configure>", self._resize_text, add="+")

    def _resize_text(self, _event: Any = None) -> None:
        available = max(40, self.winfo_width() / self._get_widget_scaling() - 292)
        for label, value in ((self.name_label, self.path.name), (self.detail_label, self.detail_text)):
            font = label.cget("font")
            shortened = value
            while len(shortened) > 5 and font.measure(shortened) > available:
                keep = len(shortened) - 2
                shortened = value[:keep // 2] + "…" + value[-(keep // 2):]
            label.configure(text=shortened)

    def show_details(self) -> None:
        message = f"Source: {self.path}\n\n{self.detail_text}"
        if self.output_path:
            message += f"\n\nOutput: {self.output_path}"
        messagebox.showinfo(self.path.name, message, parent=self.winfo_toplevel())

    def set_selected(self, selected: bool) -> None:
        self.configure(
            border_width=2 if selected else 1,
            border_color=COLORS["blue"] if selected else COLORS["border"],
        )

    def _ready_detail(self) -> str:
        if self.kind == "folder":
            return "Complete folder clone • PDFs optimized; other files preserved"
        try:
            return f"{format_bytes(self.path.stat().st_size)}  •  {_shorten(str(self.path.parent), 58)}"
        except OSError:
            return _shorten(str(self.path.parent), 58)

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        if self.output_path and not locked:
            self.action_button.configure(state="normal", text="Folder", command=self._open_output)
        else:
            self.action_button.configure(
                state="disabled" if locked else "normal",
                text="Remove",
                command=lambda: self._on_remove(self.path),
            )

    def set_status(self, text: str, tone: str = "blue", detail: str | None = None) -> None:
        palettes = {
            "blue": (COLORS["blue_soft"], COLORS["blue"]),
            "green": (COLORS["green_soft"], COLORS["green"]),
            "amber": (COLORS["amber_soft"], COLORS["amber"]),
            "red": (COLORS["red_soft"], COLORS["red"]),
        }
        background, foreground = palettes[tone]
        self.status_label.configure(text=text, fg_color=background, text_color=foreground)
        if detail:
            self.detail_text = detail
            self._resize_text()

    def reset(self) -> None:
        self.output_path = None
        self.set_status("Ready", "blue", self._ready_detail())
        self.action_button.configure(text="Remove", command=lambda: self._on_remove(self.path))

    def show_result(
        self,
        result: engine.OptimizationResult | folders.FolderOptimizationResult,
    ) -> None:
        status_value = getattr(result.status, "value", str(result.status)).lower()
        output_path = getattr(result, "output_path", None)
        self.output_path = Path(output_path) if output_path else None

        if isinstance(result, folders.FolderOptimizationResult) and self.output_path:
            detail = (
                f"{result.pdf_total} PDF{'s' if result.pdf_total != 1 else ''}"
                f"  •  {result.other_files_copied} other file"
                f"{'s' if result.other_files_copied != 1 else ''}"
                f"  •  Saved {format_bytes(result.saved_bytes)}"
            )
            self.set_status("Folder cloned", "green", detail)
            if result.copied_pdf_count:
                self.set_status(
                    "Folder cloned", "amber",
                    detail + f" • {result.copied_pdf_count} PDFs copied unchanged (could not optimize)",
                )
        elif status_value == "optimized":
            content = document_type_label(result.document_type)
            image_note = (
                f"  •  {result.images_optimized} image"
                f"{'s' if result.images_optimized != 1 else ''} optimized"
                if result.images_optimized
                else ""
            )
            detail = (
                f"{format_bytes(result.input_size)} → {format_bytes(result.output_size)}"
                f"  •  {result.saved_percent:.1f}% smaller  •  {content}{image_note}"
            )
            self.set_status("Optimized", "green", detail)
        elif "signed" in status_value:
            self.set_status("Signed · skipped", "amber", result.message)
        elif "cancel" in status_value:
            self.set_status("Canceled", "amber", result.message)
        else:
            content = document_type_label(result.document_type)
            detail = f"{format_bytes(result.input_size)}  •  Already efficient  •  {content}"
            self.set_status("Already optimal", "amber", detail)

        if self.output_path:
            self.action_button.configure(state="normal", text="Folder", command=self._open_output)

    def show_error(self, message: str, skipped: bool = False) -> None:
        self.output_path = None
        self.set_status("Skipped" if skipped else "Failed", "amber" if skipped else "red", message)
        self.action_button.configure(
            state="disabled" if self._locked else "normal",
            text="Remove",
            command=lambda: self._on_remove(self.path),
        )

    def _open_output(self) -> None:
        if not self.output_path:
            return
        try:
            _open_folder(self.output_path)
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc))


class PDFOptimizerApp(_BaseWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self._dnd_available = False
        if _HAS_DND_PACKAGE:
            try:
                TkinterDnD.require(self)
                self._dnd_available = True
            except (RuntimeError, TclError):
                # Drag/drop is optional; the picker remains fully functional.
                self._dnd_available = False
        self.title("PDF Optimizer")
        self.geometry(self._centered_geometry(1180, 760))
        self.minsize(960, 660)
        self.configure(fg_color=COLORS["background"])

        self.paths: list[Path] = []
        self.rows: dict[Path, FileRow] = {}
        self.item_kinds: dict[Path, str] = {}
        self.selected_path: Path | None = None
        self.selected_output_dir: Path | None = None
        self.is_running = False
        self.close_when_stopped = False
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.batch_results: list[
            engine.OptimizationResult | folders.FolderOptimizationResult
        ] = []
        self.batch_errors = 0
        self.batch_skipped = 0
        self._header_compact: bool | None = None
        self._wrap_width: int | None = None
        self.batch_index = 0
        self.batch_total = 0
        self.batch_level = "medium"
        self.records: dict[Path, workflow.ResultRecord] = {}
        self.preferences_file = workflow.preferences_path()
        self._appearance = "System"

        self._build_layout()
        self._build_menus()
        self._apply_preferences(workflow.load_preferences(self.preferences_file))
        self._bind_shortcuts()
        self.bind("<Configure>", self._on_window_configure, add="+")
        self.after_idle(self._apply_responsive_header)
        self._enable_drag_and_drop()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)

    def _current_preferences(self) -> workflow.Preferences:
        return workflow.Preferences(
            self.compression_choice.get().lower(), self._appearance,
            str(self.selected_output_dir) if self.selected_output_dir else None,
        )

    def _save_preferences(self) -> None:
        try:
            workflow.save_preferences(self.preferences_file, self._current_preferences())
        except OSError as exc:
            self.status_detail.configure(text=f"Could not remember settings: {exc}")

    def _apply_preferences(self, preferences: workflow.Preferences) -> None:
        self._appearance = preferences.appearance
        ctk.set_appearance_mode(preferences.appearance)
        self.compression_choice.set(preferences.compression_level.title())
        self._compression_changed(self.compression_choice.get(), persist=False)
        self.selected_output_dir = Path(preferences.output_dir) if preferences.output_dir else None
        self.output_choice.set("Choose folder" if self.selected_output_dir else "Beside originals")
        if self.selected_output_dir:
            self.output_detail.configure(text=str(self.selected_output_dir))
            self.output_browse_button.pack(side="right", padx=(8, 0))
        else:
            self.output_browse_button.pack_forget()
            self.output_detail.configure(text="PDFs use _optimized; folders become complete _optimized clones")

    def _centered_geometry(self, width: int, height: int) -> str:
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 18)
        return f"{width}x{height}+{x}+{y}"

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, minsize=286)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew", padx=(32, 34), pady=28)
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_drop_zone()
        self._build_queue()
        self._build_output_options()
        self._build_action_bar()
        self.main.bind("<Configure>", self._resize_content, add="+")

    def _resize_content(self, _event: Any = None) -> None:
        width = int(self.main.winfo_width() / self.main._get_widget_scaling())
        if width <= 1 or width == self._wrap_width:
            return
        self._wrap_width = width
        settings_wrap = max(150, width - 360)
        self.compression_detail.configure(wraplength=settings_wrap)
        self.output_detail.configure(wraplength=settings_wrap)
        status_wrap = max(150, width - (340 if self.is_running else 240))
        self.status_detail.configure(wraplength=status_wrap)

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=286, corner_radius=0, fg_color=COLORS["sidebar"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(7, weight=1)

        logo = ctk.CTkLabel(
            sidebar,
            width=44,
            height=44,
            corner_radius=12,
            fg_color="#2E6FF2",
            text="P",
            text_color="#FFFFFF",
            font=ctk.CTkFont(FONT_FAMILY, 21, "bold"),
        )
        logo.grid(row=0, column=0, sticky="w", padx=28, pady=(31, 10))

        ctk.CTkLabel(
            sidebar,
            text="PDF Optimizer",
            anchor="w",
            text_color=COLORS["sidebar_text"],
            font=ctk.CTkFont(FONT_FAMILY, 22, "bold"),
        ).grid(row=1, column=0, sticky="ew", padx=28)
        ctk.CTkLabel(
            sidebar,
            text="Smaller files. You choose the quality.",
            anchor="w",
            text_color=COLORS["sidebar_muted"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).grid(row=2, column=0, sticky="ew", padx=28, pady=(2, 28))

        guarantee = ctk.CTkFrame(sidebar, corner_radius=14, fg_color=COLORS["sidebar_card"])
        guarantee.grid(row=3, column=0, sticky="ew", padx=22)
        ctk.CTkLabel(
            guarantee,
            text="SMART AUTO MODE",
            anchor="w",
            text_color="#72E1B3",
            font=ctk.CTkFont(FONT_FAMILY, 10, "bold"),
        ).pack(fill="x", padx=17, pady=(15, 3))
        ctk.CTkLabel(
            guarantee,
            text="Text and vectors stay crisp. Large\nimages follow your chosen level.",
            anchor="w",
            justify="left",
            text_color=COLORS["sidebar_text"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).pack(fill="x", padx=17, pady=(0, 15))

        benefits = ctk.CTkFrame(sidebar, fg_color="transparent")
        benefits.grid(row=4, column=0, sticky="ew", padx=28, pady=(27, 0))
        for text in (
            "Original files stay untouched",
            "Folder structure is preserved",
            "Processing stays on this PC",
        ):
            item = ctk.CTkFrame(benefits, fg_color="transparent")
            item.pack(fill="x", pady=5)
            ctk.CTkLabel(
                item,
                width=19,
                text="✓",
                text_color="#72E1B3",
                font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            ).pack(side="left")
            ctk.CTkLabel(
                item,
                text=text,
                anchor="w",
                text_color=COLORS["sidebar_muted"],
                font=ctk.CTkFont(FONT_FAMILY, 11),
            ).pack(side="left", padx=(7, 0))

        note = ctk.CTkLabel(
            sidebar,
            text="Minimum is fully lossless. Medium is\nthe recommended everyday setting.",
            justify="left",
            anchor="w",
            text_color="#8FA5BF",
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        note.grid(row=6, column=0, sticky="sw", padx=28, pady=(20, 0))

        self.theme_button = ctk.CTkButton(
            sidebar,
            height=38,
            corner_radius=8,
            text="Switch appearance",
            fg_color=COLORS["sidebar_card"],
            hover_color="#244363",
            border_width=0,
            text_color=COLORS["sidebar_muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
            command=self._toggle_appearance,
        )
        self.theme_button.grid(row=8, column=0, sticky="sew", padx=22, pady=(12, 24))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.grid_columnconfigure(0, weight=1)

        self.header_title = ctk.CTkLabel(
            header,
            text="Compress without compromise",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 27, "bold"),
        )
        self.header_title.grid(row=0, column=0, sticky="w")
        self.header_subtitle = ctk.CTkLabel(
            header,
            text="Automatically adapt to text PDFs, scans, images, and full folders.",
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        )
        self.header_subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.header_add_folder_button = ctk.CTkButton(
            header,
            width=112,
            height=40,
            corner_radius=8,
            text="Add folder",
            fg_color=COLORS["button"],
            hover_color=COLORS["button_hover"],
            border_width=0,
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self._choose_folder,
        )
        self.header_add_folder_button.grid(row=0, column=1, rowspan=2, padx=(20, 8))

        self.header_add_button = ctk.CTkButton(
            header,
            width=108,
            height=40,
            corner_radius=8,
            text="Add PDFs",
            fg_color=COLORS["blue"],
            hover_color=COLORS["blue_hover"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self._choose_files,
        )
        self.header_add_button.grid(row=0, column=2, rowspan=2)

        self.more_button = ctk.CTkButton(
            header,
            width=42,
            height=40,
            corner_radius=8,
            text="•••",
            fg_color=COLORS["button"],
            hover_color=COLORS["button_hover"],
            border_width=0,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
            command=self._show_more_menu,
        )
        self.more_button.grid(row=0, column=3, rowspan=2, padx=(8, 0))

    def _build_drop_zone(self) -> None:
        self.drop_zone = ctk.CTkFrame(
            self.main,
            height=140,
            corner_radius=16,
            fg_color=COLORS["surface"],
            border_width=2,
            border_color=COLORS["border"],
        )
        self.drop_zone.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.drop_zone.grid_propagate(False)
        self.drop_zone.grid_columnconfigure(0, weight=1)

        self.drop_title = ctk.CTkLabel(
            self.drop_zone,
            text="Drop PDF files or a folder here" if self._dnd_available else "Add PDF files or a folder",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 15, "bold"),
        )
        self.drop_title.grid(row=0, column=0, pady=(16, 2))
        drop_hint = "A folder is cloned completely; its PDFs are optimized in place in the clone"
        if not self._dnd_available:
            drop_hint = "Use Add PDFs or Add folder to choose what to optimize"
        self.drop_hint = ctk.CTkLabel(
            self.drop_zone,
            text=drop_hint,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
        )
        self.drop_hint.grid(row=1, column=0)
        self.browse_button = ctk.CTkButton(
            self.drop_zone,
            width=106,
            height=32,
            corner_radius=8,
            text="Browse PDFs",
            fg_color=COLORS["button"],
            hover_color=COLORS["button_hover"],
            border_width=0,
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
            command=self._choose_files,
        )
        self.browse_button.grid(row=2, column=0, pady=(10, 20))

    def _build_queue(self) -> None:
        card = ctk.CTkFrame(
            self.main,
            corner_radius=16,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        card.grid(row=2, column=0, sticky="nsew", pady=(0, 16))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        queue_header = ctk.CTkFrame(card, fg_color="transparent", height=44)
        queue_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 0))
        queue_header.grid_columnconfigure(2, weight=1)
        self.queue_title = ctk.CTkLabel(
            queue_header,
            text="Queue",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
        )
        self.queue_title.grid(row=0, column=0, sticky="w")
        self.queue_count = ctk.CTkLabel(
            queue_header,
            width=54,
            height=24,
            corner_radius=12,
            text="0 items",
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 9, "bold"),
        )
        self.queue_count.grid(row=0, column=1, sticky="w", padx=(9, 0))
        self.clear_button = ctk.CTkButton(
            queue_header,
            width=76,
            height=29,
            text="Clear queue",
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 10),
            state="disabled",
            command=self._clear_files,
        )
        self.retry_button = ctk.CTkButton(
            queue_header, text="Retry unfinished", width=112, height=29,
            corner_radius=8, fg_color=COLORS["button"],
            hover_color=COLORS["button_hover"], text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 10), command=self._retry_unfinished,
        )
        self.retry_button.grid(row=0, column=3, padx=(0, 6))
        self.retry_button.grid_remove()
        self.export_button = ctk.CTkButton(
            queue_header, text="Export CSV", width=86, height=29,
            corner_radius=8, fg_color=COLORS["button"],
            hover_color=COLORS["button_hover"], text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 10), command=self._export_results,
        )
        self.export_button.grid(row=0, column=4, padx=(0, 6))
        self.export_button.grid_remove()
        self.clear_button.grid(row=0, column=5, sticky="e")
        self.clear_button.grid_remove()

        self.file_list = ctk.CTkScrollableFrame(
            card,
            fg_color="transparent",
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["subtle"],
        )
        self.file_list.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=(2, 10))
        self.file_list.grid_columnconfigure(0, weight=1)
        self.file_list.grid_remove()

        self.empty_state = ctk.CTkFrame(card, fg_color="transparent")
        self.empty_state.grid(row=1, column=0, sticky="nsew", padx=10, pady=(2, 10))
        self.empty_label = ctk.CTkLabel(
            self.empty_state,
            text="No items yet — add PDFs or a folder above",
            text_color=COLORS["subtle"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        )
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def _build_output_options(self) -> None:
        self.settings_card = ctk.CTkFrame(
            self.main,
            corner_radius=14,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        settings = self.settings_card
        settings.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        settings.grid_columnconfigure(0, weight=1)

        compression_label = ctk.CTkFrame(settings, fg_color="transparent")
        compression_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 7))
        ctk.CTkLabel(
            compression_label,
            text="Compression level",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        ).pack(fill="x")
        self.compression_detail = ctk.CTkLabel(
            compression_label,
            text="Recommended • high-quality images; text stays crisp",
            anchor="w",
            text_color=COLORS["muted"],
            justify="left",
            wraplength=260,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        self.compression_detail.pack(fill="x", pady=(1, 0))

        self.compression_choice = ctk.StringVar(value="Medium")
        self.compression_switch = ChoiceControl(
            settings,
            values=["Minimum", "Medium", "Strong"],
            variable=self.compression_choice,
            command=self._compression_changed,
        )
        self.compression_switch.grid(row=0, column=2, sticky="e", padx=16, pady=(12, 7))

        separator = ctk.CTkFrame(settings, height=1, fg_color=COLORS["border"])
        separator.grid(row=1, column=0, columnspan=3, sticky="ew", padx=16)

        output_label = ctk.CTkFrame(settings, fg_color="transparent")
        output_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 13))
        output_heading = ctk.CTkFrame(output_label, fg_color="transparent")
        output_heading.pack(fill="x")
        ctk.CTkLabel(
            output_heading,
            text="Output location",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        ).pack(side="left")
        self.output_browse_button = ctk.CTkButton(
            output_heading, text="Change…", width=64, height=24,
            fg_color="transparent", text_color=COLORS["blue"],
            hover_color=COLORS["blue_soft"],
            command=lambda: self._output_mode_changed("Choose folder"),
        )
        self.output_detail = ctk.CTkLabel(
            output_label,
            text="PDFs use _optimized; folders become complete _optimized clones",
            anchor="w",
            text_color=COLORS["muted"],
            justify="left",
            wraplength=260,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        self.output_detail.pack(fill="x", pady=(2, 0))

        self.output_choice = ctk.StringVar(value="Beside originals")
        self.output_switch = ChoiceControl(
            settings,
            values=["Beside originals", "Choose folder"],
            variable=self.output_choice,
            command=self._output_mode_changed,
        )
        self.output_switch.grid(row=2, column=2, sticky="e", padx=16, pady=(8, 13))

    def _build_action_bar(self) -> None:
        action = ctk.CTkFrame(
            self.main,
            height=78,
            corner_radius=14,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        action.grid(row=4, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)

        status = ctk.CTkFrame(action, fg_color="transparent")
        status.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=13)
        self.status_title = ctk.CTkLabel(
            status,
            text="Ready to optimize",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        )
        self.status_title.pack(fill="x")
        self.status_detail = ctk.CTkLabel(
            status,
            text="Add PDF files or a folder to begin",
            anchor="w",
            text_color=COLORS["muted"],
            justify="left",
            wraplength=460,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        self.status_detail.pack(fill="x", pady=(1, 5))
        self.progress = ctk.CTkProgressBar(
            status,
            width=260,
            height=4,
            corner_radius=2,
            fg_color=COLORS["border"],
            progress_color=COLORS["blue"],
        )
        self.progress.pack(fill="x")
        self.progress.set(0)
        self.progress.pack_forget()

        self.cancel_button = ctk.CTkButton(
            action,
            width=92,
            height=44,
            corner_radius=8,
            text="Cancel",
            fg_color=COLORS["red_soft"],
            hover_color=COLORS["red_soft"],
            border_width=0,
            text_color=COLORS["red"],
            font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
            state="disabled",
            command=self._cancel_batch,
        )
        self.cancel_button.grid(row=0, column=1, padx=(0, 10), pady=16)
        self.cancel_button.grid_remove()

        self.optimize_button = ctk.CTkButton(
            action,
            width=176,
            height=44,
            corner_radius=8,
            text="Optimize",
            fg_color=COLORS["surface_alt"],
            hover_color=COLORS["blue_hover"],
            text_color="#FFFFFF",
            text_color_disabled=COLORS["subtle"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            state="disabled",
            command=self._start_batch,
        )
        self.optimize_button.grid(row=0, column=2, padx=(0, 14), pady=16)

    def _build_menus(self) -> None:
        self.more_menu = Menu(self, tearoff=False)
        self.more_menu.add_command(
            label="Add PDF files…", accelerator="Ctrl+O", command=self._choose_files
        )
        self.more_menu.add_command(
            label="Add a folder…", accelerator="Ctrl+Shift+O", command=self._choose_folder
        )
        self.more_menu.add_separator()
        self.queue_menu = Menu(self.more_menu, tearoff=False)
        self.queue_menu.add_command(label="Save queue…", accelerator="Ctrl+S", command=self._save_queue)
        self.queue_menu.add_command(label="Load queue…", accelerator="Ctrl+L", command=self._load_queue)
        self.queue_menu.add_separator()
        self.queue_menu.add_command(label="Move selected up", accelerator="Alt+Up", command=lambda: self._move_selected(-1))
        self.queue_menu.add_command(label="Move selected down", accelerator="Alt+Down", command=lambda: self._move_selected(1))
        self.more_menu.add_cascade(label="Queue", menu=self.queue_menu)
        self.more_menu.add_command(label="Optimize selected", command=self._optimize_selected)
        self.more_menu.add_command(label="Retry unfinished", command=self._retry_unfinished)
        self.more_menu.add_command(label="Export results as CSV…", command=self._export_results)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Open original", command=lambda: self._open_selected_document(False))
        self.more_menu.add_command(label="Open optimized copy", command=lambda: self._open_selected_document(True))
        self.more_menu.add_command(
            label="Open selected location", command=self._open_selected_location
        )
        self.more_menu.add_command(label="Copy selected path", command=self._copy_selected_path)
        self.more_menu.add_command(label="View selected details", command=self._show_selected_details)
        self.more_menu.add_command(
            label="Remove selected", accelerator="Delete", command=self._remove_selected
        )
        self.more_menu.add_command(label="Clear queue", command=self._clear_files)
        self.more_menu.add_separator()
        self.more_menu.add_command(
            label="Optimize queue", accelerator="Ctrl+Enter", command=self._start_batch
        )
        self.more_menu.add_command(label="Cancel optimization", accelerator="Esc", command=self._cancel_batch)
        self.more_menu.add_separator()
        self.more_menu.add_command(
            label="Keyboard shortcuts", accelerator="F1", command=self._show_shortcuts
        )
        self.more_menu.add_command(label="About PDF Optimizer", command=self._show_about)
        self.more_menu.add_command(label="Reset saved preferences", command=self._reset_preferences)

    def _set_more_menu_states(self) -> None:
        can_edit = not self.is_running
        has_items = bool(self.paths)
        has_selection = self.selected_path in self.rows
        for label, enabled in (
            ("Save queue…", can_edit and has_items),
            ("Load queue…", can_edit),
            ("Move selected up", can_edit and has_selection and self.paths.index(self.selected_path) > 0),
            ("Move selected down", can_edit and has_selection and self.paths.index(self.selected_path) < len(self.paths) - 1),
        ):
            self.queue_menu.entryconfigure(label, state="normal" if enabled else "disabled")
        for label, enabled in (
            ("Optimize selected", can_edit and has_selection),
            ("Retry unfinished", can_edit and bool(self._retry_paths())),
            ("Export results as CSV…", can_edit and bool(self.records)),
            ("Open original", has_selection),
            ("Open optimized copy", has_selection and bool(self.rows[self.selected_path].output_path)),
            ("Reset saved preferences", can_edit),
        ):
            self.more_menu.entryconfigure(label, state="normal" if enabled else "disabled")
        for label in ("Add PDF files…", "Add a folder…"):
            self.more_menu.entryconfigure(label, state="normal" if can_edit else "disabled")
        for label in ("Open selected location", "Copy selected path", "View selected details"):
            self.more_menu.entryconfigure(label, state="normal" if has_selection else "disabled")
        self.more_menu.entryconfigure(
            "Remove selected", state="normal" if can_edit and has_selection else "disabled"
        )
        self.more_menu.entryconfigure(
            "Clear queue", state="normal" if can_edit and has_items else "disabled"
        )
        self.more_menu.entryconfigure(
            "Optimize queue", state="normal" if can_edit and has_items else "disabled"
        )
        self.more_menu.entryconfigure(
            "Cancel optimization", state="normal" if self.is_running else "disabled"
        )

    def _show_more_menu(self) -> None:
        self._set_more_menu_states()
        self.update_idletasks()
        x = self.more_button.winfo_rootx() + self.more_button.winfo_width()
        y = self.more_button.winfo_rooty() + self.more_button.winfo_height() + 3
        try:
            self.more_menu.tk_popup(x, y)
        finally:
            self.more_menu.grab_release()

    def _retry_paths(self) -> list[Path]:
        return [path for path in self.paths if path in self.records and self.records[path].status in {"failed", "cancelled"}]

    def _update_workflow_actions(self) -> None:
        for button, visible in (
            (self.retry_button, bool(self._retry_paths())),
            (self.export_button, bool(self.records)),
        ):
            if visible:
                button.grid()
            else:
                button.grid_remove()
            button.configure(state="disabled" if self.is_running else "normal")

    def _optimize_selected(self) -> None:
        if self.selected_path in self.rows:
            self._start_batch([self.selected_path])

    def _retry_unfinished(self) -> None:
        paths = self._retry_paths()
        if paths:
            self._start_batch(paths)

    def _move_selected(self, offset: int) -> None:
        if self.is_running or self.selected_path not in self.paths:
            return
        index = self.paths.index(self.selected_path)
        destination = index + offset
        if 0 <= destination < len(self.paths):
            self.paths.insert(destination, self.paths.pop(index))
            self._refresh_file_rows()

    def _open_selected_document(self, optimized: bool) -> None:
        if self.selected_path not in self.rows:
            return
        path = self.rows[self.selected_path].output_path if optimized else self.selected_path
        if path is not None:
            try:
                _open_document(path)
            except OSError as exc:
                messagebox.showerror("Could not open document", str(exc), parent=self)

    def _valid_export_path(self, chosen: str, extension: str) -> Path | None:
        path = Path(chosen).resolve()
        protected = self.paths + [row.output_path for row in self.rows.values() if row.output_path]
        if path.suffix.lower() != extension or path in protected:
            messagebox.showerror("Choose another filename", f"Choose a new {extension} file, separate from source documents and outputs.", parent=self)
            return None
        return path

    def _save_queue(self) -> None:
        if self.is_running or not self.paths:
            return
        chosen = filedialog.asksaveasfilename(parent=self, title="Save queue", defaultextension=".json", initialfile="PDF Optimizer queue.json", filetypes=[("PDF Optimizer queue", "*.json")])
        if not chosen:
            return
        try:
            path = self._valid_export_path(chosen, ".json")
            if path is None:
                return
            workflow.save_queue(path, self.paths, self._current_preferences())
            self.status_detail.configure(text=f"Saved queue to {path.name}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save queue", str(exc), parent=self)

    def _load_queue(self) -> None:
        if self.is_running:
            return
        chosen = filedialog.askopenfilename(parent=self, title="Load queue", filetypes=[("PDF Optimizer queue", "*.json")])
        if not chosen:
            return
        try:
            saved = workflow.load_queue(Path(chosen).resolve())
            valid = [path for path in saved.paths if path.is_dir() or (path.is_file() and path.suffix.lower() == ".pdf")]
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("Could not load queue", str(exc), parent=self)
            return
        if not valid:
            messagebox.showinfo("No available sources", "The saved queue has no available PDFs or folders. Your current queue was kept.", parent=self)
            return
        self._clear_files()
        self._apply_preferences(saved.preferences)
        self._add_paths(valid)
        self._save_preferences()
        missing = len(saved.paths) - len(valid)
        self.status_detail.configure(text=f"Loaded {len(self.paths)} items • {missing} missing or unsupported paths ignored")

    def _export_results(self) -> None:
        if self.is_running or not self.records:
            return
        chosen = filedialog.asksaveasfilename(parent=self, title="Export results", defaultextension=".csv", initialfile="PDF optimization results.csv", filetypes=[("CSV report", "*.csv")])
        if not chosen:
            return
        try:
            path = self._valid_export_path(chosen, ".csv")
            if path is None:
                return
            workflow.export_results(path, [self.records[path] for path in self.paths if path in self.records])
            self.status_detail.configure(text=f"Exported {len(self.records)} results to {path.name}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not export results", str(exc), parent=self)

    def _reset_preferences(self) -> None:
        if not self.is_running:
            self._apply_preferences(workflow.Preferences())
            self._save_preferences()
            self.status_detail.configure(text="Preferences reset to Medium compression, system appearance, and beside-originals output")

    def _open_selected_location(self) -> None:
        if self.selected_path not in self.rows:
            return
        try:
            _open_folder(self.selected_path)
        except OSError as exc:
            messagebox.showerror("Could not open location", str(exc), parent=self)

    def _copy_selected_path(self) -> None:
        if self.selected_path not in self.rows:
            return
        self.clipboard_clear()
        self.clipboard_append(str(self.selected_path))
        self.status_detail.configure(text="Selected source path copied to the clipboard")

    def _show_shortcuts(self) -> None:
        messagebox.showinfo(
            "Keyboard shortcuts",
            "Ctrl+O        Add PDF files\n"
            "Ctrl+Shift+O  Add a folder\n"
            "Ctrl+Enter    Optimize the queue\n"
            "Ctrl+S        Save queue\n"
            "Ctrl+L        Load queue\n"
            "Alt+Up/Down   Move selected item\n"
            "Delete        Remove the selected item\n"
            "Double-click  View full item details\n"
            "Esc           Cancel optimization\n"
            "F1            Show this help",
            parent=self,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About PDF Optimizer",
            "PDF Optimizer\n\nSmart local compression for PDFs and complete folder clones.\n"
            "Your source files remain untouched.",
            parent=self,
        )

    def _show_selected_details(self) -> None:
        if self.selected_path in self.rows:
            self.rows[self.selected_path].show_details()

    def _on_window_configure(self, event: Any) -> None:
        if event.widget is self:
            self._apply_responsive_header()

    def _apply_responsive_header(self, logical_width: float | None = None) -> None:
        if logical_width is None:
            logical_width = self.winfo_width() / self._get_window_scaling()
        compact = logical_width < 1080
        if compact == self._header_compact:
            return
        self._header_compact = compact
        self.header_title.configure(
            font=ctk.CTkFont(FONT_FAMILY, 21 if compact else 27, "bold")
        )
        self.header_subtitle.configure(
            text=(
                "Smart optimization for PDFs, scans, and folders."
                if compact
                else "Automatically adapt to text PDFs, scans, images, and full folders."
            )
        )

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-o>", lambda _event: self._choose_files())
        self.bind_all("<Control-O>", lambda _event: self._choose_files())
        self.bind_all("<Control-Shift-O>", lambda _event: self._choose_folder())
        self.bind_all("<Control-Return>", lambda _event: self._start_batch())
        self.bind_all("<Control-s>", lambda _event: self._save_queue())
        self.bind_all("<Control-l>", lambda _event: self._load_queue())
        self.bind_all("<Alt-Up>", lambda _event: self._move_selected(-1))
        self.bind_all("<Alt-Down>", lambda _event: self._move_selected(1))
        self.bind_all("<Delete>", lambda _event: self._remove_selected())
        self.bind_all("<Escape>", lambda _event: self._cancel_batch())
        self.bind_all("<F1>", lambda _event: self._show_shortcuts())

    def _enable_drag_and_drop(self) -> None:
        if not self._dnd_available:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._handle_drop)
        except Exception:  # noqa: BLE001 - optional native integration boundary
            # A missing platform tkdnd binary should not prevent normal file picking.
            self._dnd_available = False
            self.drop_title.configure(text="Add PDF files or a folder")
            self.drop_hint.configure(text="Use Add PDFs or Add folder to choose what to optimize")

    def _handle_drop(self, event: Any) -> str:
        if self.is_running:
            return "break"
        try:
            dropped = [Path(value) for value in self.tk.splitlist(event.data)]
        except (TclError, TypeError, AttributeError):
            dropped = []
        self._add_paths(dropped)
        return "break"

    def _choose_files(self) -> None:
        if self.is_running:
            return
        selected = filedialog.askopenfilenames(
            parent=self,
            title="Choose PDF files",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        self._add_paths(Path(value) for value in selected)

    def _choose_folder(self) -> None:
        if self.is_running:
            return
        selected = filedialog.askdirectory(parent=self, title="Choose a folder to clone")
        if selected:
            self._add_paths([Path(selected)])

    def _add_paths(self, paths: Any) -> None:
        if self.is_running:
            return

        existing = {os.path.normcase(str(path)) for path in self.paths}
        added = 0
        rejected = 0
        merged = 0
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                rejected += 1
                continue
            key = os.path.normcase(str(resolved))
            is_pdf = resolved.is_file() and resolved.suffix.lower() == ".pdf"
            is_folder = resolved.is_dir()
            if not is_pdf and not is_folder:
                rejected += 1
                continue
            if key in existing:
                continue

            # An already queued parent folder owns every PDF/nested folder
            # below it, so adding the child separately would duplicate work.
            if any(
                self.item_kinds.get(existing_path) == "folder"
                and _path_is_within(resolved, existing_path)
                for existing_path in self.paths
            ):
                continue

            # Adding a parent folder merges any previously queued children.
            if is_folder:
                nested_items = [
                    existing_path
                    for existing_path in self.paths
                    if _path_is_within(existing_path, resolved)
                ]
                for nested_item in nested_items:
                    self._remove_file(nested_item)
                    merged += 1
                existing = {os.path.normcase(str(path)) for path in self.paths}

            existing.add(key)
            self.paths.append(resolved)
            kind = "folder" if is_folder else "pdf"
            self.item_kinds[resolved] = kind
            row = FileRow(
                self.file_list,
                resolved,
                self._remove_file,
                self._select_file,
                kind=kind,
            )
            self.rows[resolved] = row
            added += 1

        if added:
            self._refresh_file_rows()
            self._select_file(self.paths[-1])
            self.status_title.configure(text=f"{len(self.paths)} item{'s' if len(self.paths) != 1 else ''} ready")
            detail = "Sources stay untouched; folder clones preserve every file and subfolder"
            if merged:
                detail += f"  •  Merged {merged} nested item{'s' if merged != 1 else ''}"
            self.status_detail.configure(text=detail)
            self.progress.set(0)
            self.progress.pack_forget()
        if rejected:
            self.status_detail.configure(
                text=f"Ignored {rejected} item{'s' if rejected != 1 else ''} that were not PDFs or folders"
            )

    def _refresh_file_rows(self) -> None:
        self._update_workflow_actions()
        if self.paths:
            self.empty_state.grid_remove()
            self.file_list.grid()
            self.drop_zone.grid_remove()
        else:
            self.file_list.grid_remove()
            self.empty_state.grid()
            self.drop_zone.grid()

        for index, path in enumerate(self.paths):
            self.rows[path].grid(row=index, column=0, sticky="ew", padx=2, pady=(2, 6))

        count = len(self.paths)
        self.queue_count.configure(
            text=f"{count} item{'s' if count != 1 else ''}",
            fg_color=COLORS["blue_soft"] if count else COLORS["surface_alt"],
            text_color=COLORS["blue"] if count else COLORS["muted"],
        )
        self.clear_button.configure(state="normal" if count and not self.is_running else "disabled")
        if count:
            self.clear_button.grid()
        else:
            self.clear_button.grid_remove()
        can_optimize = bool(count and not self.is_running)
        self.optimize_button.configure(
            state="normal" if can_optimize else "disabled",
            fg_color=COLORS["blue"] if can_optimize else COLORS["surface_alt"],
        )
        self.optimize_button.configure(
            text=f"Optimize {count} item{'s' if count != 1 else ''}"
            if count
            else "Optimize"
        )

    def _select_file(self, path: Path) -> None:
        if path not in self.rows:
            return
        self.selected_path = path
        for row_path, row in self.rows.items():
            row.set_selected(row_path == path)

    def _remove_selected(self) -> None:
        if self.selected_path:
            self._remove_file(self.selected_path)

    def _remove_file(self, path: Path) -> None:
        if self.is_running or path not in self.rows:
            return
        row = self.rows.pop(path)
        row.destroy()
        self.paths.remove(path)
        self.item_kinds.pop(path, None)
        self.records.pop(path, None)
        if self.selected_path == path:
            self.selected_path = self.paths[-1] if self.paths else None
        self._refresh_file_rows()
        if self.selected_path:
            self._select_file(self.selected_path)
        if not self.paths:
            self.status_title.configure(text="Ready to optimize")
            self.status_detail.configure(text="Add PDF files or a folder to begin")
            self.progress.set(0)
            self.progress.pack_forget()

    def _clear_files(self) -> None:
        if self.is_running:
            return
        for row in self.rows.values():
            row.destroy()
        self.paths.clear()
        self.rows.clear()
        self.item_kinds.clear()
        self.records.clear()
        self.selected_path = None
        self._refresh_file_rows()
        self.status_title.configure(text="Ready to optimize")
        self.status_detail.configure(text="Add PDF files or a folder to begin")
        self.progress.set(0)
        self.progress.pack_forget()

    def _output_mode_changed(self, value: str) -> None:
        if self.is_running:
            return
        if value == "Choose folder":
            chosen = filedialog.askdirectory(parent=self, title="Choose an output folder")
            if not chosen:
                mode = "Choose folder" if self.selected_output_dir else "Beside originals"
                self.output_choice.set(mode)
                self.output_switch.set(mode)
                return
            self.selected_output_dir = Path(chosen).resolve()
            self.output_choice.set("Choose folder")
            self.output_switch.set("Choose folder")
            self.output_detail.configure(text=str(self.selected_output_dir))
            self.output_browse_button.pack(side="right", padx=(8, 0))
        else:
            self.output_choice.set("Beside originals")
            self.selected_output_dir = None
            self.output_browse_button.pack_forget()
            self.output_detail.configure(
                text="PDFs use _optimized; folders become complete _optimized clones"
            )
        self._save_preferences()

    def _compression_changed(self, value: str, persist: bool = True) -> None:
        descriptions = {
            "Minimum": "Fully lossless • structural compression only",
            "Medium": "Recommended • high-quality images; text stays crisp",
            "Strong": "Smallest scans • image softness may be visible when zoomed",
        }
        self.compression_detail.configure(text=descriptions[value])
        if persist:
            self._save_preferences()

    def _start_batch(self, selected_paths: list[Path] | None = None) -> None:
        if self.is_running or not self.paths:
            return
        selected = set(self.paths if selected_paths is None else selected_paths)
        paths_snapshot = [path for path in self.paths if path in selected]
        if not paths_snapshot:
            return
        if self.output_choice.get() == "Choose folder" and not self.selected_output_dir:
            self._output_mode_changed("Choose folder")
            if not self.selected_output_dir:
                return
        if self.selected_output_dir and self.output_choice.get() == "Choose folder":
            unsafe_source = next(
                (
                    path
                    for path in paths_snapshot
                    if self.item_kinds.get(path) == "folder"
                    and _path_is_within(self.selected_output_dir, path)
                ),
                None,
            )
            if unsafe_source:
                messagebox.showerror(
                    "Choose a different output folder",
                    "A folder clone cannot be created inside its source tree. "
                    f"Choose a location outside:\n{unsafe_source}",
                    parent=self,
                )
                return

        self.is_running = True
        self.close_when_stopped = False
        self.cancel_event.clear()
        self.batch_results = []
        self.batch_errors = 0
        self.batch_skipped = 0

        for path, row in self.rows.items():
            if path in selected:
                row.reset()
                self.records.pop(path, None)
            row.set_locked(True)

        self._set_controls_running(True)
        level = CompressionLevel(self.compression_choice.get().lower())
        self.batch_level = level.value
        options = engine.OptimizationOptions(compression_level=level)
        self.status_title.configure(text="Preparing smart optimization…")
        self.status_detail.configure(text=f"Item 1 of {len(paths_snapshot)}  •  {level.value.title()} compression")
        self.progress.pack(fill="x")
        self.batch_index = 0
        self.batch_total = len(paths_snapshot)
        self.progress.configure(mode="determinate")
        self.progress.set(0)

        output_dir = self.selected_output_dir if self.output_choice.get() == "Choose folder" else None
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(paths_snapshot, dict(self.item_kinds), output_dir, options),
            name="pdf-optimizer-worker",
            daemon=True,
        )
        self.worker.start()

    def _run_batch(
        self,
        paths: list[Path],
        item_kinds: dict[Path, str],
        output_dir: Path | None,
        options: engine.OptimizationOptions,
    ) -> None:
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            if self.cancel_event.is_set():
                self.events.put(("canceled_remaining", paths[index - 1 :]))
                break

            item_kind = item_kinds.get(path, "pdf")
            self.events.put(("file_start", path, index, total, item_kind))

            def report_stage(stage: engine.OptimizationStage, current_path: Path = path) -> None:
                self.events.put(("stage", current_path, stage))

            def report_folder_progress(
                progress: folders.FolderProgress,
                current_path: Path = path,
            ) -> None:
                self.events.put(("folder_progress", current_path, progress))

            try:
                if item_kind == "folder":
                    result = folders.clone_and_optimize_folder(
                        path,
                        output_parent=output_dir,
                        options=options,
                        cancel_event=self.cancel_event,
                        progress_callback=report_folder_progress,
                    )
                else:
                    result = engine.optimize_pdf(
                        path,
                        output_dir=output_dir,
                        options=options,
                        cancel_event=self.cancel_event,
                        progress_callback=report_stage,
                    )
                self.events.put(("result", path, result))
            except Exception as exc:  # noqa: BLE001 - isolate each batch item
                self.events.put(("error", path, exc))

            if self.cancel_event.is_set():
                self.events.put(("canceled_remaining", paths[index:]))
                break

        self.events.put(("batch_done", self.cancel_event.is_set()))

    def _drain_events(self) -> None:
        try:
            # Yield to input/paint events even when a large folder produces a
            # continuous stream of worker updates.
            for _ in range(100):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "file_start":
                    _, path, index, total, item_kind = event
                    self.batch_index, self.batch_total = index, total
                    self.progress.set((index - 1) / total)
                    action = "Cloning" if item_kind == "folder" else "Optimizing"
                    self.status_title.configure(text=f"{action} {_shorten(path.name, 42)}")
                    self.status_detail.configure(text=f"Item {index} of {total}  •  Checking content")
                    self.rows[path].set_status("Scanning" if item_kind == "folder" else "Checking", "blue")
                elif kind == "stage":
                    _, path, stage = event
                    self._show_stage(path, stage)
                elif kind == "folder_progress":
                    _, path, progress = event
                    self._show_folder_progress(path, progress)
                elif kind == "result":
                    _, path, result = event
                    self.batch_results.append(result)
                    self.records[path] = workflow.ResultRecord.from_result(result)
                    self.rows[path].show_result(result)
                    self.progress.set(self.batch_index / max(1, self.batch_total))
                    value = getattr(result.status, "value", str(result.status)).lower()
                    if "signed" in value or "cancel" in value:
                        self.batch_skipped += 1
                elif kind == "error":
                    _, path, exc = event
                    class_name = type(exc).__name__.lower()
                    skipped = "encrypted" in class_name or "signed" in class_name
                    self.records[path] = workflow.ResultRecord.incomplete(
                        path, self.item_kinds[path], "skipped" if skipped else "failed",
                        self.batch_level, str(exc),
                    )
                    self.rows[path].show_error(str(exc), skipped=skipped)
                    self.progress.set(self.batch_index / max(1, self.batch_total))
                    if skipped:
                        self.batch_skipped += 1
                    else:
                        self.batch_errors += 1
                elif kind == "canceled_remaining":
                    for path in event[1]:
                        if path in self.rows:
                            self.rows[path].set_status("Canceled", "amber", "Not started")
                            self.records[path] = workflow.ResultRecord.incomplete(
                                path, self.item_kinds[path], "cancelled", self.batch_level, "Not started",
                            )
                elif kind == "batch_done":
                    self._finish_batch(bool(event[1]))
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - protect the Tk event loop
            # Keep the event loop alive and surface unexpected presentation errors.
            self.status_title.configure(text="A display error occurred")
            self.status_detail.configure(text=str(exc))
        finally:
            # Closing still needs to drain the worker's completion event.
            if self.is_running or not self.close_when_stopped:
                try:
                    if self.winfo_exists():
                        self.after(80, self._drain_events)
                except TclError:
                    pass

    def _show_folder_progress(
        self,
        path: Path,
        progress: folders.FolderProgress,
    ) -> None:
        if path not in self.rows:
            return
        stage = progress.stage
        if self.batch_total and progress.total_files:
            fraction = progress.completed_files / progress.total_files
            self.progress.set((self.batch_index - 1 + fraction) / self.batch_total)
        current = _shorten(str(progress.current_path), 52) if progress.current_path else ""
        count = (
            f"{min(progress.completed_files + 1, progress.total_files)} of {progress.total_files}"
            if progress.total_files
            else ""
        )
        if stage is folders.FolderStage.SCANNING:
            label, detail, tone = "Scanning", "Reading the complete folder structure", "blue"
        elif stage is folders.FolderStage.COPYING:
            label = "Cloning"
            detail = f"Copying {current}  •  {count}" if count else f"Copying {current}"
            tone = "blue"
        elif stage is folders.FolderStage.OPTIMIZING_PDF:
            label = "Optimizing PDFs"
            detail = f"{current}  •  {count}" if count else current
            tone = "blue"
        elif stage is folders.FolderStage.FINALIZING:
            label, detail, tone = "Finalizing", "Publishing the completed clone safely", "blue"
        elif stage is folders.FolderStage.CANCELLED:
            label, detail, tone = "Canceled", "No partial clone was kept", "amber"
        else:
            return
        self.rows[path].set_status(label, tone)
        self.status_detail.configure(text=detail)

    def _show_stage(self, path: Path, stage: Any) -> None:
        if path not in self.rows:
            return
        value = getattr(stage, "value", str(stage)).lower()
        stage_fraction = {"checking": 0.05, "optimizing": 0.25, "verifying": 0.8, "finalizing": 0.95}
        if self.batch_total and value in stage_fraction:
            self.progress.set((self.batch_index - 1 + stage_fraction[value]) / self.batch_total)
        if "check" in value:
            label, detail = "Checking", "Checking document"
        elif "optim" in value:
            label, detail = "Optimizing", "Compressing PDF structure"
        elif "verify" in value:
            label, detail = "Verifying", "Validating the optimized copy"
        elif "final" in value:
            label, detail = "Saving", "Safely writing the output"
        elif "signed" in value:
            label, detail = "Signed · skipped", "Digital signatures must remain untouched"
        elif "cancel" in value:
            label, detail = "Canceled", "Stopped safely"
        else:
            return
        self.rows[path].set_status(label, "amber" if "skip" in label.lower() or "cancel" in label.lower() else "blue")
        self.status_detail.configure(text=detail)

    def _finish_batch(self, was_canceled: bool) -> None:
        self.is_running = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self._set_controls_running(False)
        for row in self.rows.values():
            row.set_locked(False)

        successful_results = [result for result in self.batch_results if result.output_path]
        saved = sum(max(0, int(result.saved_bytes)) for result in successful_results)
        input_total = sum(max(0, int(result.input_size)) for result in successful_results)
        percent = (saved / input_total * 100) if input_total else 0.0
        optimized = sum(
            result.optimized_count
            if isinstance(result, folders.FolderOptimizationResult)
            else 1
            for result in self.batch_results
            if getattr(result.status, "value", str(result.status)).lower() == "optimized"
        )
        folder_clones = sum(
            1
            for result in successful_results
            if isinstance(result, folders.FolderOptimizationResult)
        )
        completed_items = len(successful_results)
        images_optimized = sum(result.images_optimized for result in successful_results)

        if was_canceled:
            self.status_title.configure(text="Optimization canceled safely")
            self.status_detail.configure(text=f"Completed outputs were kept  •  Saved {format_bytes(saved)}")
            self.progress.set(completed_items / max(1, self.batch_total))
        elif self.batch_errors:
            self.status_title.configure(text=f"Finished with {self.batch_errors} error{'s' if self.batch_errors != 1 else ''}")
            self.status_detail.configure(
                text=f"{completed_items} completed  •  {self.batch_skipped} skipped  •  Saved {format_bytes(saved)}"
            )
            self.progress.set(1)
        else:
            self.status_title.configure(text="Optimization complete")
            detail = f"{completed_items} item{'s' if completed_items != 1 else ''} completed"
            if folder_clones:
                detail += f"  •  {folder_clones} folder{'s' if folder_clones != 1 else ''} cloned"
            detail += f"  •  {optimized} PDF{'s' if optimized != 1 else ''} reduced"
            if images_optimized:
                detail += f"  •  {images_optimized} image{'s' if images_optimized != 1 else ''} optimized"
            detail += f"  •  Saved {format_bytes(saved)} ({percent:.1f}%)"
            if self.batch_skipped:
                detail += f"  •  {self.batch_skipped} skipped"
            self.status_detail.configure(text=detail)
            self.progress.set(1)

        if self.close_when_stopped:
            self.destroy()

    def _set_controls_running(self, running: bool) -> None:
        self._update_workflow_actions()
        normal_state = "disabled" if running else "normal"
        self.header_add_button.configure(state=normal_state)
        self.header_add_folder_button.configure(state=normal_state)
        self.browse_button.configure(state=normal_state)
        self.clear_button.configure(state="disabled" if running or not self.paths else "normal")
        self.output_switch.configure(state=normal_state)
        self.output_browse_button.configure(state=normal_state)
        self.compression_switch.configure(state=normal_state)
        can_optimize = bool(self.paths and not running)
        self.optimize_button.configure(
            state="normal" if can_optimize else "disabled",
            fg_color=COLORS["blue"] if can_optimize else COLORS["surface_alt"],
        )
        self.cancel_button.configure(state="normal" if running else "disabled", text="Cancel")
        if running:
            self.cancel_button.grid()
        else:
            self.cancel_button.grid_remove()
        self._wrap_width = None
        self._resize_content()

    def _cancel_batch(self) -> None:
        if not self.is_running or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled", text="Stopping…")
        self.status_title.configure(text="Stopping safely…")
        self.status_detail.configure(text="Temporary PDF and folder-clone data will be removed safely")

    def _toggle_appearance(self) -> None:
        current = ctk.get_appearance_mode().lower()
        self._appearance = "Light" if current == "dark" else "Dark"
        ctk.set_appearance_mode(self._appearance)
        self._save_preferences()

    def _on_close(self) -> None:
        if not self.is_running:
            self.destroy()
            return
        should_close = messagebox.askyesno(
            "Stop optimization?",
            "The current task will be stopped safely. Completed output files will be kept.",
            parent=self,
        )
        if should_close:
            self.close_when_stopped = True
            self._cancel_batch()


__all__ = ["FileRow", "PDFOptimizerApp", "format_bytes"]
