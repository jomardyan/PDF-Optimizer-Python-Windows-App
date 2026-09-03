from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific DPI behavior")
def test_launcher_enables_per_monitor_dpi_awareness() -> None:
    script = """
import ctypes

from app_dearpygui import _enable_windows_dpi_awareness

_enable_windows_dpi_awareness()
awareness = ctypes.c_int(-1)
result = ctypes.WinDLL("shcore").GetProcessDpiAwareness(
    None,
    ctypes.byref(awareness),
)
assert result == 0
assert awareness.value == 2
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
