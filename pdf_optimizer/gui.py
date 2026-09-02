"""Modern batch desktop interface for the lossless PDF optimizer."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from tkinter import TclError, filedialog, messagebox
from typing import Any

import customtkinter as ctk

from . import optimizer as engine

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


def _open_folder(path: Path) -> None:
    folder = path if path.is_dir() else path.parent
    if sys.platform == "win32":
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


if _HAS_DND_PACKAGE:

    class _BaseWindow(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        """CustomTkinter root with optional drag/drop methods mixed in."""

else:

    class _BaseWindow(ctk.CTk):
        """CustomTkinter root used when drag/drop is not installed."""


class FileRow(ctk.CTkFrame):
    """A single queued PDF and its current/result state."""

    def __init__(
        self,
        master: Any,
        path: Path,
        on_remove: Callable[[Path], None],
        on_select: Callable[[Path], None],
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
        self.output_path: Path | None = None
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
            fg_color=COLORS["blue_soft"],
            text="PDF",
            text_color=COLORS["blue"],
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

        try:
            file_size = path.stat().st_size
            detail = f"{format_bytes(file_size)}  •  {_shorten(str(path.parent), 58)}"
        except OSError:
            detail = _shorten(str(path.parent), 58)
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
            width=105,
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

    def set_selected(self, selected: bool) -> None:
        self.configure(
            border_width=2 if selected else 1,
            border_color=COLORS["blue"] if selected else COLORS["border"],
        )

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
            self.detail_label.configure(text=detail)

    def reset(self) -> None:
        self.output_path = None
        try:
            detail = f"{format_bytes(self.path.stat().st_size)}  •  {_shorten(str(self.path.parent), 58)}"
        except OSError:
            detail = _shorten(str(self.path.parent), 58)
        self.set_status("Ready", "blue", detail)
        self.action_button.configure(text="Remove", command=lambda: self._on_remove(self.path))

    def show_result(self, result: engine.OptimizationResult) -> None:
        status_value = getattr(result.status, "value", str(result.status)).lower()
        output_path = getattr(result, "output_path", None)
        self.output_path = Path(output_path) if output_path else None

        if status_value == "optimized":
            detail = (
                f"{format_bytes(result.input_size)} → {format_bytes(result.output_size)}"
                f"  •  {result.saved_percent:.1f}% smaller"
            )
            self.set_status("Optimized", "green", detail)
        elif "signed" in status_value:
            self.set_status("Signed · skipped", "amber", result.message)
        elif "cancel" in status_value:
            self.set_status("Canceled", "amber", result.message)
        else:
            detail = f"{format_bytes(result.input_size)}  •  No smaller lossless version found"
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
        self.selected_path: Path | None = None
        self.selected_output_dir: Path | None = None
        self.is_running = False
        self.close_when_stopped = False
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.batch_results: list[engine.OptimizationResult] = []
        self.batch_errors = 0
        self.batch_skipped = 0

        self._build_layout()
        self._bind_shortcuts()
        self._enable_drag_and_drop()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)

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
            text="Smaller files. Same visuals.",
            anchor="w",
            text_color=COLORS["sidebar_muted"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).grid(row=2, column=0, sticky="ew", padx=28, pady=(2, 28))

        guarantee = ctk.CTkFrame(sidebar, corner_radius=14, fg_color=COLORS["sidebar_card"])
        guarantee.grid(row=3, column=0, sticky="ew", padx=22)
        ctk.CTkLabel(
            guarantee,
            text="TRUE LOSSLESS",
            anchor="w",
            text_color="#72E1B3",
            font=ctk.CTkFont(FONT_FAMILY, 10, "bold"),
        ).pack(fill="x", padx=17, pady=(15, 3))
        ctk.CTkLabel(
            guarantee,
            text="Images, fonts, and page content\nare never re-encoded.",
            anchor="w",
            justify="left",
            text_color=COLORS["sidebar_text"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).pack(fill="x", padx=17, pady=(0, 15))

        benefits = ctk.CTkFrame(sidebar, fg_color="transparent")
        benefits.grid(row=4, column=0, sticky="ew", padx=28, pady=(27, 0))
        for text in (
            "Original files stay untouched",
            "Processing stays on this PC",
            "Every output is validated",
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
            text="Lossless savings depend on how the\noriginal PDF was created.",
            justify="left",
            anchor="w",
            text_color="#8FA5BF",
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        note.grid(row=6, column=0, sticky="sw", padx=28, pady=(20, 0))

        self.theme_button = ctk.CTkButton(
            sidebar,
            height=38,
            corner_radius=10,
            text="Switch appearance",
            fg_color="transparent",
            hover_color=COLORS["sidebar_card"],
            border_width=1,
            border_color="#39506D",
            text_color=COLORS["sidebar_muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
            command=self._toggle_appearance,
        )
        self.theme_button.grid(row=8, column=0, sticky="sew", padx=22, pady=(12, 24))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Compress without compromise",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 27, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="Losslessly optimize one PDF or a whole batch.",
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.header_add_button = ctk.CTkButton(
            header,
            width=112,
            height=40,
            corner_radius=10,
            text="+  Add PDFs",
            fg_color=COLORS["blue"],
            hover_color=COLORS["blue_hover"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self._choose_files,
        )
        self.header_add_button.grid(row=0, column=1, rowspan=2, padx=(20, 0))

    def _build_drop_zone(self) -> None:
        self.drop_zone = ctk.CTkFrame(
            self.main,
            height=126,
            corner_radius=16,
            fg_color=COLORS["surface"],
            border_width=2,
            border_color=COLORS["border"],
        )
        self.drop_zone.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.drop_zone.grid_propagate(False)
        self.drop_zone.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.drop_zone,
            text="Drop PDF files here",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 15, "bold"),
        ).grid(row=0, column=0, pady=(23, 2))
        drop_hint = "Drag several files at once, or browse from your computer"
        if not self._dnd_available:
            drop_hint = "Browse and select several PDF files at once"
        ctk.CTkLabel(
            self.drop_zone,
            text=drop_hint,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 11),
        ).grid(row=1, column=0)
        self.browse_button = ctk.CTkButton(
            self.drop_zone,
            width=106,
            height=32,
            corner_radius=8,
            text="Browse PDFs",
            fg_color="transparent",
            hover_color=COLORS["blue_soft"],
            border_width=1,
            border_color=COLORS["blue"],
            text_color=COLORS["blue"],
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
        queue_header.grid_columnconfigure(0, weight=1)
        self.queue_title = ctk.CTkLabel(
            queue_header,
            text="Files  ·  0",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
        )
        self.queue_title.grid(row=0, column=0, sticky="w")
        self.clear_button = ctk.CTkButton(
            queue_header,
            width=70,
            height=29,
            text="Clear all",
            fg_color="transparent",
            hover_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 10),
            state="disabled",
            command=self._clear_files,
        )
        self.clear_button.grid(row=0, column=1, sticky="e")

        self.file_list = ctk.CTkScrollableFrame(
            card,
            fg_color="transparent",
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["subtle"],
        )
        self.file_list.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=(2, 10))
        self.file_list.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.file_list,
            text="Your PDF queue will appear here",
            text_color=COLORS["subtle"],
            font=ctk.CTkFont(FONT_FAMILY, 12),
        )
        self.empty_label.grid(row=0, column=0, sticky="nsew", pady=32)

    def _build_output_options(self) -> None:
        output = ctk.CTkFrame(
            self.main,
            height=80,
            corner_radius=14,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
        )
        output.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        output.grid_propagate(False)
        output.grid_columnconfigure(1, weight=1)

        label_block = ctk.CTkFrame(output, fg_color="transparent")
        label_block.grid(row=0, column=0, sticky="w", padx=16, pady=12)
        ctk.CTkLabel(
            label_block,
            text="Output location",
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        ).pack(fill="x")
        self.output_detail = ctk.CTkLabel(
            label_block,
            text="New files use the suffix _optimized",
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        self.output_detail.pack(fill="x", pady=(2, 0))

        self.output_choice = ctk.StringVar(value="Beside originals")
        self.output_switch = ctk.CTkSegmentedButton(
            output,
            width=278,
            height=36,
            corner_radius=9,
            values=["Beside originals", "Choose folder"],
            variable=self.output_choice,
            selected_color=COLORS["blue"],
            selected_hover_color=COLORS["blue_hover"],
            unselected_color=COLORS["surface_alt"],
            unselected_hover_color=COLORS["blue_soft"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(FONT_FAMILY, 10, "bold"),
            command=self._output_mode_changed,
        )
        self.output_switch.grid(row=0, column=2, sticky="e", padx=16, pady=20)

    def _build_action_bar(self) -> None:
        action = ctk.CTkFrame(self.main, height=58, fg_color="transparent")
        action.grid(row=4, column=0, sticky="ew")
        action.grid_columnconfigure(0, weight=1)

        status = ctk.CTkFrame(action, fg_color="transparent")
        status.grid(row=0, column=0, sticky="w")
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
            text="Add one or more PDF files to begin",
            anchor="w",
            text_color=COLORS["muted"],
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

        self.cancel_button = ctk.CTkButton(
            action,
            width=92,
            height=42,
            corner_radius=10,
            text="Cancel",
            fg_color="transparent",
            hover_color=COLORS["red_soft"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["red"],
            font=ctk.CTkFont(FONT_FAMILY, 11, "bold"),
            state="disabled",
            command=self._cancel_batch,
        )
        self.cancel_button.grid(row=0, column=1, padx=(18, 10))

        self.optimize_button = ctk.CTkButton(
            action,
            width=164,
            height=42,
            corner_radius=10,
            text="Optimize PDFs",
            fg_color=COLORS["blue"],
            hover_color=COLORS["blue_hover"],
            text_color="#FFFFFF",
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            state="disabled",
            command=self._start_batch,
        )
        self.optimize_button.grid(row=0, column=2)

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-o>", lambda _event: self._choose_files())
        self.bind_all("<Control-O>", lambda _event: self._choose_files())
        self.bind_all("<Control-Return>", lambda _event: self._start_batch())
        self.bind_all("<Delete>", lambda _event: self._remove_selected())
        self.bind_all("<Escape>", lambda _event: self._cancel_batch())

    def _enable_drag_and_drop(self) -> None:
        if not self._dnd_available:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._handle_drop)
        except Exception:  # noqa: BLE001 - optional native integration boundary
            # A missing platform tkdnd binary should not prevent normal file picking.
            self._dnd_available = False

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

    def _add_paths(self, paths: Any) -> None:
        if self.is_running:
            return

        existing = {os.path.normcase(str(path.resolve())) for path in self.paths}
        added = 0
        rejected = 0
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                rejected += 1
                continue
            key = os.path.normcase(str(resolved))
            if not resolved.is_file() or resolved.suffix.lower() != ".pdf":
                rejected += 1
                continue
            if key in existing:
                continue
            existing.add(key)
            self.paths.append(resolved)
            row = FileRow(self.file_list, resolved, self._remove_file, self._select_file)
            self.rows[resolved] = row
            added += 1

        if added:
            self._refresh_file_rows()
            self._select_file(self.paths[-1])
            self.status_title.configure(text=f"{len(self.paths)} PDF{'s' if len(self.paths) != 1 else ''} ready")
            self.status_detail.configure(text="Lossless optimization will never alter the originals")
            self.progress.set(0)
        if rejected:
            self.status_detail.configure(text=f"Ignored {rejected} item{'s' if rejected != 1 else ''} that were not PDF files")

    def _refresh_file_rows(self) -> None:
        if self.paths:
            self.empty_label.grid_remove()
        else:
            self.empty_label.grid()

        for index, path in enumerate(self.paths):
            self.rows[path].grid(row=index, column=0, sticky="ew", padx=2, pady=(2, 6))

        count = len(self.paths)
        self.queue_title.configure(text=f"Files  ·  {count}")
        self.clear_button.configure(state="normal" if count and not self.is_running else "disabled")
        self.optimize_button.configure(state="normal" if count and not self.is_running else "disabled")
        self.optimize_button.configure(text=f"Optimize {count} PDF{'s' if count != 1 else ''}" if count else "Optimize PDFs")

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
        if self.selected_path == path:
            self.selected_path = self.paths[-1] if self.paths else None
        self._refresh_file_rows()
        if self.selected_path:
            self._select_file(self.selected_path)
        if not self.paths:
            self.status_title.configure(text="Ready to optimize")
            self.status_detail.configure(text="Add one or more PDF files to begin")
            self.progress.set(0)

    def _clear_files(self) -> None:
        if self.is_running:
            return
        for row in self.rows.values():
            row.destroy()
        self.paths.clear()
        self.rows.clear()
        self.selected_path = None
        self._refresh_file_rows()
        self.status_title.configure(text="Ready to optimize")
        self.status_detail.configure(text="Add one or more PDF files to begin")
        self.progress.set(0)

    def _output_mode_changed(self, value: str) -> None:
        if value == "Choose folder":
            chosen = filedialog.askdirectory(parent=self, title="Choose an output folder")
            if not chosen:
                self.output_choice.set("Beside originals")
                self.output_switch.set("Beside originals")
                return
            self.selected_output_dir = Path(chosen).resolve()
            self.output_detail.configure(text=_shorten(str(self.selected_output_dir), 70))
        else:
            self.output_detail.configure(text="New files use the suffix _optimized")

    def _start_batch(self) -> None:
        if self.is_running or not self.paths:
            return
        if self.output_choice.get() == "Choose folder" and not self.selected_output_dir:
            self._output_mode_changed("Choose folder")
            if not self.selected_output_dir:
                return

        self.is_running = True
        self.close_when_stopped = False
        self.cancel_event.clear()
        self.batch_results = []
        self.batch_errors = 0
        self.batch_skipped = 0

        for row in self.rows.values():
            row.reset()
            row.set_locked(True)

        self._set_controls_running(True)
        self.status_title.configure(text="Preparing lossless optimization…")
        self.status_detail.configure(text=f"File 1 of {len(self.paths)}")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

        paths_snapshot = list(self.paths)
        output_dir = self.selected_output_dir if self.output_choice.get() == "Choose folder" else None
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(paths_snapshot, output_dir),
            name="pdf-optimizer-worker",
            daemon=True,
        )
        self.worker.start()

    def _run_batch(self, paths: list[Path], output_dir: Path | None) -> None:
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            if self.cancel_event.is_set():
                self.events.put(("canceled_remaining", paths[index - 1 :]))
                break

            self.events.put(("file_start", path, index, total))

            def report_stage(stage: engine.OptimizationStage, current_path: Path = path) -> None:
                self.events.put(("stage", current_path, stage))

            try:
                result = engine.optimize_pdf(
                    path,
                    output_dir=output_dir,
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
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "file_start":
                    _, path, index, total = event
                    self.status_title.configure(text=f"Optimizing {_shorten(path.name, 42)}")
                    self.status_detail.configure(text=f"File {index} of {total}  •  Checking document")
                    self.rows[path].set_status("Checking", "blue")
                elif kind == "stage":
                    _, path, stage = event
                    self._show_stage(path, stage)
                elif kind == "result":
                    _, path, result = event
                    self.batch_results.append(result)
                    self.rows[path].show_result(result)
                    value = getattr(result.status, "value", str(result.status)).lower()
                    if "signed" in value or "cancel" in value:
                        self.batch_skipped += 1
                elif kind == "error":
                    _, path, exc = event
                    class_name = type(exc).__name__.lower()
                    skipped = "encrypted" in class_name or "signed" in class_name
                    self.rows[path].show_error(str(exc), skipped=skipped)
                    if skipped:
                        self.batch_skipped += 1
                    else:
                        self.batch_errors += 1
                elif kind == "canceled_remaining":
                    for path in event[1]:
                        if path in self.rows:
                            self.rows[path].set_status("Canceled", "amber", "Not started")
                elif kind == "batch_done":
                    self._finish_batch(bool(event[1]))
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - protect the Tk event loop
            # Keep the event loop alive and surface unexpected presentation errors.
            self.status_title.configure(text="A display error occurred")
            self.status_detail.configure(text=str(exc))
        finally:
            if not self.close_when_stopped:
                try:
                    if self.winfo_exists():
                        self.after(80, self._drain_events)
                except TclError:
                    pass

    def _show_stage(self, path: Path, stage: Any) -> None:
        if path not in self.rows:
            return
        value = getattr(stage, "value", str(stage)).lower()
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
            1
            for result in self.batch_results
            if getattr(result.status, "value", str(result.status)).lower() == "optimized"
        )

        if was_canceled:
            self.status_title.configure(text="Optimization canceled safely")
            self.status_detail.configure(text=f"Completed outputs were kept  •  Saved {format_bytes(saved)}")
            self.progress.set(0)
        elif self.batch_errors:
            self.status_title.configure(text=f"Finished with {self.batch_errors} error{'s' if self.batch_errors != 1 else ''}")
            self.status_detail.configure(text=f"{optimized} optimized  •  {self.batch_skipped} skipped  •  Saved {format_bytes(saved)}")
            self.progress.set(1)
        else:
            self.status_title.configure(text="Optimization complete")
            detail = f"{optimized} reduced  •  Saved {format_bytes(saved)} ({percent:.1f}%)"
            if self.batch_skipped:
                detail += f"  •  {self.batch_skipped} skipped"
            self.status_detail.configure(text=detail)
            self.progress.set(1)

        if self.close_when_stopped:
            self.destroy()

    def _set_controls_running(self, running: bool) -> None:
        normal_state = "disabled" if running else "normal"
        self.header_add_button.configure(state=normal_state)
        self.browse_button.configure(state=normal_state)
        self.clear_button.configure(state="disabled" if running or not self.paths else "normal")
        self.output_switch.configure(state=normal_state)
        self.optimize_button.configure(state="disabled" if running or not self.paths else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled", text="Cancel")

    def _cancel_batch(self) -> None:
        if not self.is_running or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled", text="Stopping…")
        self.status_title.configure(text="Stopping safely…")
        self.status_detail.configure(text="The current write will finish before temporary files are removed")

    def _toggle_appearance(self) -> None:
        current = ctk.get_appearance_mode().lower()
        ctk.set_appearance_mode("Light" if current == "dark" else "Dark")

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
