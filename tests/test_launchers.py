from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launchers")
@pytest.mark.parametrize("launcher", ["run.bat", "build.bat"])
def test_first_run_uses_discovered_python_launcher(tmp_path: Path, launcher: str) -> None:
    project = Path(__file__).resolve().parents[1]
    script = tmp_path / launcher
    script.write_bytes((project / launcher).read_bytes())
    for name in ("app.py", "requirements.txt", "requirements-dev.txt", "pdf_optimizer.spec"):
        (tmp_path / name).touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "py.cmd").write_text(
        '@echo off\nif "%~2"=="-c" exit /b 0\n'
        'echo selected>"%~dp0selected.txt"\nexit /b 1\n'
    )
    (fake_bin / "python.cmd").write_text('@echo off\nexit /b 1\n')
    env = os.environ.copy()
    env.pop("PY_LAUNCHER", None)
    env["PATH"] = str(fake_bin) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(script)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert (fake_bin / "selected.txt").exists(), result.stdout + result.stderr
    assert result.returncode != 0  # The stub intentionally stops before installation.
