from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sluice import __version__
from sluice.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_config_shows_sources(write_config: Callable[..., Path]) -> None:
    result = runner.invoke(app, ["config", "-c", str(write_config("log_level: debug\n"))])
    assert result.exit_code == 0
    assert "log_level = DEBUG  (file)" in result.stdout


def test_config_error_exits_2(write_config: Callable[..., Path]) -> None:
    result = runner.invoke(app, ["config", "-c", str(write_config("log_level: loud\n"))])
    assert result.exit_code == 2
    assert "log_level" in result.stderr


def test_secrets_are_masked(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    from sluice import cli
    from sluice.config import SETTINGS, Setting

    secret = Setting(SETTINGS[0].key, SETTINGS[0].default, SETTINGS[0].help, secret=True)
    monkeypatch.setattr(cli, "SETTINGS", (secret, *SETTINGS[1:]))
    result = runner.invoke(app, ["config", "-c", str(write_config(""))])
    assert "log_level = ********" in result.stdout


def test_main_runs_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from sluice import cli

    called: list[bool] = []
    monkeypatch.setattr(cli, "app", lambda: called.append(True))
    cli.main()
    assert called == [True]
