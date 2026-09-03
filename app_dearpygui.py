"""Dear PyGui entry point for PDF Optimizer."""

from __future__ import annotations

import multiprocessing
import sys


def _enable_windows_dpi_awareness() -> None:
    """Prevent Windows from bitmap-scaling and blurring the GPU viewport."""
    if sys.platform != "win32":
        return

    try:
        import ctypes
    except ImportError:
        return

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = (ctypes.c_void_p,)
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):  # Per-monitor DPI aware, version 2.
            return
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore")
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = (ctypes.c_int,)
        set_awareness.restype = ctypes.c_long
        result = set_awareness(2)  # Per-monitor DPI aware on Windows 8.1.
        if result in (0, -2147024891):  # Success or already configured.
            return
    except (AttributeError, OSError):
        pass

    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def main() -> int:
    _enable_windows_dpi_awareness()
    try:
        from pdf_optimizer.dearpygui_gui import run_app
    except ModuleNotFoundError as exc:
        missing = exc.name or "a required package"
        print(
            f"PDF Optimizer could not start because {missing!r} is missing.\n"
            "Run run_dearpygui.bat, or install the packages with:\n"
            "    python -m pip install -r requirements-dearpygui.txt",
            file=sys.stderr,
        )
        return 1

    return run_app(smoke_test="--smoke-test" in sys.argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
