from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Tests and their GUI subprocesses must never read or change user settings."""
    monkeypatch.setenv("PDF_OPTIMIZER_CONFIG_DIR", str(tmp_path / "config"))
