from collections.abc import Callable, Iterator
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


def test_config_shows_every_setting(write_config: Callable[..., Path]) -> None:
    from sluice.config import SETTINGS

    result = runner.invoke(app, ["config", "-c", str(write_config("io:\n  threads: 3\n"))])
    assert result.exit_code == 0
    for setting in SETTINGS:
        assert f"{setting.key} = " in result.stdout
    assert "io.threads = 3  (file)" in result.stdout
    assert "None" not in result.stdout


APP = """
from sluice import Sluice, VirtualHandler, access_log, auth


async def decode(ctx, raw):
    return raw


app = Sluice()
app.use(access_log())
app.param("token", decode, secret=True)
user = app.route("/fuse/{user}/{token}", backend="/ext4/{user}", middlewares=[auth()])
user.route("private")
user.route("status", virtual=True, handler=VirtualHandler())
"""


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "cliapp.py").write_text(APP, encoding="utf-8")
    monkeypatch.setenv("SLUICE_APP_MODULE", "cliapp:app")
    monkeypatch.setenv("SLUICE_APP_DIR", str(tmp_path))
    monkeypatch.setenv("SLUICE_SECRETS_DIR", str(tmp_path / "no-secrets"))
    monkeypatch.setenv("SLUICE_CONFIG", str(tmp_path / "none.yaml"))
    yield tmp_path
    import sys

    sys.modules.pop("cliapp", None)
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


def test_routes_prints_tree(app_env: Path) -> None:
    result = runner.invoke(app, ["routes"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "/fuse/{user}/{token}  [backend /ext4/{user}]"
    assert lines[1] == "  middleware: access_log(), auth(allow_all)"
    inherited = "[inherits /ext4/{user} from /fuse/{user}/{token}]"
    assert f"  /fuse/{{user}}/{{token}}/private  {inherited}" in lines
    assert "  /fuse/{user}/{token}/status  [virtual]" in lines


def test_resolve_masks_secret_params(app_env: Path) -> None:
    result = runner.invoke(app, ["resolve", "/fuse/alice/T0k/private/notes.txt", "--op", "read"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "path:       /fuse/alice/********/private/notes.txt" in out
    assert "params:     user=alice, token=********" in out
    assert "real path:  /ext4/alice/private/notes.txt" in out
    assert "backend of: /fuse/{user}/{token}" in out
    assert "param():    token" in out
    assert "middleware: access_log(), auth(allow_all)  (for read)" in out
    assert "T0k" not in out


def test_resolve_synthetic_unmatched_and_refused(app_env: Path) -> None:
    result = runner.invoke(app, ["resolve", "/fuse/alice/T0k"])
    assert "children:   private, status" in result.stdout
    assert "no route: ENOENT" in runner.invoke(app, ["resolve", "/elsewhere"]).stdout
    refused = runner.invoke(app, ["resolve", "/fuse/../T0k/x"])
    assert refused.exit_code == 0
    assert "refused: EACCES" in refused.stdout
    root = runner.invoke(app, ["resolve", "/"])
    assert "params:     (none)" in root.stdout and "real path:  (none)" in root.stdout


def test_resolve_rejects_unknown_op(app_env: Path) -> None:
    result = runner.invoke(app, ["resolve", "/", "--op", "frobnicate"])
    assert result.exit_code == 2
    assert "--op" in result.stderr


def test_routes_exit_2_on_route_error(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (app_env / "badapp.py").write_text(
        "from sluice import Sluice\napp = Sluice()\napp.route('/a/{x}', backend='/b/{y}')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLUICE_APP_MODULE", "badapp:app")
    result = runner.invoke(app, ["routes"])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: /a/{x}: backend uses {y}")


def test_routes_exit_2_without_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SLUICE_CONFIG", str(tmp_path / "none.yaml"))
    result = runner.invoke(app, ["routes"])
    assert result.exit_code == 2
    assert "app.module is not set" in result.stderr


def test_mount_exit_2_on_mount_error(app_env: Path) -> None:
    result = runner.invoke(app, ["mount"])
    assert result.exit_code == 2
    assert "mount.point is not set" in result.stderr


def test_mount_serves(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sluice import mount

    served: list[str] = []
    monkeypatch.setattr(mount, "serve", lambda cfg: served.append(cfg.app_module))
    assert runner.invoke(app, ["mount"]).exit_code == 0
    assert served == ["cliapp:app"]
