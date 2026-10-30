"""A real mount: `sluice mount` in a subprocess, exercised through the kernel.

Needs /dev/fuse, fusermount3 and pyfuse3 (`uv sync --extra fuse`); skipped without them.
The app combines the requirements' four examples: multi-tenant isolation, a live status node,
redaction in a post hook, and a config file templated from a mount-time secret.
"""

import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from importlib.util import find_spec
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not Path("/dev/fuse").exists(), reason="no /dev/fuse"),
    pytest.mark.skipif(shutil.which("fusermount3") is None, reason="no fusermount3"),
    pytest.mark.skipif(find_spec("pyfuse3") is None, reason="pyfuse3 not installed"),
]

APP = """
from sluice import Deny, Middleware, Sluice, VirtualHandler, access_log, auth


async def verify(user, token, real_path, op):
    return token == "T0k"


async def decode(ctx, raw):
    return raw


class AdminOnly(Middleware):
    async def pre(self, ctx):
        if ctx.params["user"] != "admin":
            raise Deny("admins only")


class Status(VirtualHandler):
    async def render(self, ctx):
        return f"user={ctx.params['user']} op={ctx.op}\\n".encode()


class ConfigTemplate(VirtualHandler):
    async def render(self, ctx):
        secret = ctx.config.secret["db_password"].reveal()
        return f"project: {ctx.params['project']}\\npassword: {secret}\\n".encode()


class Redact(Middleware):
    ops = frozenset({"read"})

    async def post(self, ctx, result):
        return result.replace(b"SECRET", b"******")


app = Sluice()
app.use(access_log())
app.param("token", decode, secret=True)
user = app.route("/fuse/{user}/{token}", backend="BACKING/{user}", middlewares=[auth(verify)])
user.route("private", middlewares=[AdminOnly()])
user.route("status", virtual=True, handler=Status())
app.route("/fuse/{project}/config.yaml", virtual=True, handler=ConfigTemplate())
app.route("/logs/{user}", backend="BACKING/{user}", middlewares=[Redact()], direct_io=True)
"""


@pytest.fixture
def mounted(tmp_path: Path) -> Iterator[Path]:
    backing, mnt, secrets = tmp_path / "ext4", tmp_path / "mnt", tmp_path / "secrets"
    (backing / "alice" / "private").mkdir(parents=True)
    (backing / "bob").mkdir()
    (backing / "alice" / "notes.txt").write_text("hello SECRET world\n", encoding="utf-8")
    mnt.mkdir()
    secrets.mkdir()
    (secrets / "db_password").write_text("pw\n", encoding="utf-8")
    (tmp_path / "itapp.py").write_text(APP.replace("BACKING", str(backing)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLUICE_")}
    env |= {
        "SLUICE_CONFIG": str(tmp_path / "none.yaml"),
        "SLUICE_APP_MODULE": "itapp:app",
        "SLUICE_APP_DIR": str(tmp_path),
        "SLUICE_MOUNT_POINT": str(mnt),
        "SLUICE_SECRETS_DIR": str(secrets),
    }
    log = tmp_path / "sluice.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "sluice", "mount"], env=env, stdout=out, stderr=out
        )
        deadline = time.monotonic() + 15
        while not os.path.ismount(mnt):
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"mount failed:\n{log.read_text(encoding='utf-8')}")
            time.sleep(0.05)
        try:
            yield mnt
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                subprocess.run(["fusermount3", "-u", str(mnt)], check=False)  # noqa: S603, S607 - fixed arguments; cleanup after a hung daemon
                raise
    assert code == 0, log.read_text(encoding="utf-8")
    assert not os.path.ismount(mnt)
    text = log.read_text(encoding="utf-8")
    assert "T0k" not in text  # the token is a secret param: masked in every log line
    assert "Traceback" not in text, text  # no error, including at shutdown


def test_multi_tenant_read_write(mounted: Path) -> None:
    home = mounted / "fuse" / "alice" / "T0k"
    assert (home / "notes.txt").read_text(encoding="utf-8") == "hello SECRET world\n"
    (home / "new.txt").write_text("written", encoding="utf-8")
    assert (home / "new.txt").read_text(encoding="utf-8") == "written"
    assert set(os.listdir(home)) == {"notes.txt", "new.txt", "private", "status"}
    (home / "new.txt").rename(home / "renamed.txt")
    (home / "renamed.txt").unlink()
    (home / "d").mkdir()
    (home / "d").rmdir()


def test_bad_token_is_denied(mounted: Path) -> None:
    with pytest.raises(PermissionError):
        (mounted / "fuse" / "alice" / "wrong" / "notes.txt").read_text(encoding="utf-8")


def test_private_needs_admin(mounted: Path) -> None:
    with pytest.raises(PermissionError):
        os.listdir(mounted / "fuse" / "alice" / "T0k" / "private")


def test_status_node(mounted: Path) -> None:
    status = mounted / "fuse" / "alice" / "T0k" / "status"
    assert status.read_text(encoding="utf-8") == "user=alice op=open\n"
    with pytest.raises(OSError) as e:
        status.write_text("x", encoding="utf-8")
    assert e.value.errno in {errno.EROFS, errno.EACCES}


def test_templated_secret(mounted: Path) -> None:
    config = mounted / "fuse" / "proj" / "config.yaml"
    assert config.read_text(encoding="utf-8") == "project: proj\npassword: pw\n"


def test_redaction(mounted: Path) -> None:
    text = (mounted / "logs" / "alice" / "notes.txt").read_text(encoding="utf-8")
    assert text == "hello ****** world\n"


def test_synthetic_dirs(mounted: Path) -> None:
    assert sorted(os.listdir(mounted)) == ["fuse", "logs"]
    assert os.listdir(mounted / "fuse") == []
    # Every name under /fuse is a prefix of /fuse/{user}/{token}: a synthetic directory.
    assert (mounted / "fuse" / "anyone").is_dir()
    with pytest.raises(FileExistsError):
        (mounted / "fuse" / "x").mkdir()
    with pytest.raises(OSError) as e:
        os.rmdir(mounted / "fuse")
    assert e.value.errno in {errno.EROFS, errno.EACCES}
    assert not (mounted / "elsewhere").exists()


def test_rename_across_backends_is_exdev(mounted: Path) -> None:
    alice, bob = mounted / "fuse" / "alice" / "T0k", mounted / "fuse" / "bob" / "T0k"
    with pytest.raises(OSError) as e:
        os.rename(alice / "notes.txt", bob / "notes.txt")
    assert e.value.errno == errno.EXDEV


def test_open_file_outlives_unlink_and_replace(mounted: Path) -> None:
    home = mounted / "fuse" / "alice" / "T0k"
    (home / "tmp.txt").write_text("scratch", encoding="utf-8")
    with (home / "tmp.txt").open("r+b") as f:
        os.unlink(home / "tmp.txt")
        assert os.fstat(f.fileno()).st_size == 7
        os.ftruncate(f.fileno(), 3)
        assert os.fstat(f.fileno()).st_size == 3
    # The atomic-replace pattern: a reader of the old file keeps seeing the old file.
    (home / "new.txt").write_text("replacement", encoding="utf-8")
    with (home / "notes.txt").open("rb") as f:
        os.rename(home / "new.txt", home / "notes.txt")
        assert os.fstat(f.fileno()).st_size == len("hello SECRET world\n")
    assert (home / "notes.txt").read_text(encoding="utf-8") == "replacement"
