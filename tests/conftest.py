"""Shared fixtures."""

from collections.abc import Callable
from pathlib import Path

import pytest

from sluice.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees the developer's own SLUICE_* variables."""
    import os

    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[..., Path]:
    """Write a config file and return its path."""

    def write(text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return write
