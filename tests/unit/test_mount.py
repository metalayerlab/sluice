from pathlib import Path

import pytest

from sluice.config import load_config
from sluice.errors import MountError
from sluice.mount import check_mountpoint, fuse_options, prepare


def test_check_mountpoint(tmp_path: Path) -> None:
    device = tmp_path / "fuse"
    device.touch()
    point = tmp_path / "mnt"
    with pytest.raises(MountError, match="not set"):
        check_mountpoint("", device)
    with pytest.raises(MountError, match="not an existing directory"):
        check_mountpoint(str(point), device)
    point.mkdir()
    assert check_mountpoint(str(point), device) == point
    with pytest.raises(MountError, match="not found"):
        check_mountpoint(str(point), tmp_path / "absent")
    (point / "f").touch()
    with pytest.raises(MountError, match="not empty"):
        check_mountpoint(str(point), device)


def test_unreadable_mountpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    point = tmp_path / "mnt"
    point.mkdir()

    def stale(self: Path) -> None:
        raise OSError(107, "Transport endpoint is not connected")

    monkeypatch.setattr(Path, "iterdir", stale)
    with pytest.raises(MountError, match="fusermount3 -u"):
        check_mountpoint(str(point), tmp_path)


def test_fuse_options(tmp_path: Path) -> None:
    env = {"SLUICE_MOUNT_ALLOW_OTHER": "1", "SLUICE_MOUNT_DEBUG": "1"}
    cfg = load_config(tmp_path / "n.yaml", environ=env)
    opts = fuse_options(cfg, frozenset({"default_permissions"}))
    assert opts == {"default_permissions", "fsname=sluice", "allow_other", "debug"}
    plain = fuse_options(load_config(tmp_path / "n.yaml", environ={}), frozenset())
    assert plain == {"fsname=sluice"}


def test_prepare_loads_app_and_logs_secret_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "prepapp.py").write_text(
        "from sluice import Sluice\napp = Sluice()\napp.route('/x', backend='/srv')\n",
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "api_key").write_text("s3cr3t", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    env = {"SLUICE_APP_MODULE": "prepapp:app", "SLUICE_SECRETS_DIR": str(secrets)}
    cfg = load_config(tmp_path / "n.yaml", environ=env)
    import logging

    with caplog.at_level(logging.INFO):
        table = prepare(cfg)
    assert table.resolve("/x/y") is not None
    assert "api_key" in caplog.text
    assert "s3cr3t" not in caplog.text
