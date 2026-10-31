import asyncio
import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sluice import Context, FsError, MountConfig, Opened, RouteError, Secret, Sluice
from sluice.app import load_app
from sluice.config import ConfigError, load_config
from sluice.context import Attr, MkdirArgs, OpenArgs, ReadArgs, load_mount_config
from sluice.errors import AppLoadError, Deny
from sluice.virtual import SyntheticDir, VirtualHandler


@pytest.fixture
def app_dir(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "goodapp.py").write_text(
        "from sluice import Sluice\napp = Sluice()\napp.route('/x', backend='/srv')\nnot_app = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "brokenapp.py").write_text("raise ValueError('oops')\n", encoding="utf-8")
    (tmp_path / "badroute.py").write_text(
        "from sluice import Sluice\nSluice().route('/a/{x*}/b')\n", encoding="utf-8"
    )
    yield tmp_path
    for name in ("goodapp", "brokenapp", "badroute"):
        sys.modules.pop(name, None)
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


def test_load_app(app_dir: Path) -> None:
    app = load_app("goodapp:app", str(app_dir))
    assert isinstance(app, Sluice)


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ("", "not set"),
        ("goodapp", "module:attribute"),
        ("goodapp:", "module:attribute"),
        ("nosuchmodule_xyz:app", "cannot import"),
        ("brokenapp:app", "oops"),
        ("goodapp:missing", "no attribute"),
        ("goodapp:not_app", "not a Sluice app"),
    ],
)
def test_load_app_errors(app_dir: Path, spec: str, match: str) -> None:
    with pytest.raises(AppLoadError, match=match):
        load_app(spec, str(app_dir))


def test_route_error_during_import_is_kept(app_dir: Path) -> None:
    with pytest.raises(RouteError, match="last segment"):
        load_app("badroute:app", str(app_dir))


def test_secret_is_masked() -> None:
    s = Secret("hunter2")
    assert str(s) == "********"
    assert "hunter2" not in repr(s)
    assert s.reveal() == "hunter2"
    assert s == Secret("hunter2")
    assert s != "hunter2"
    assert hash(s) == hash(Secret("hunter2"))


def test_mount_config_reads_env_allowlist_and_secrets(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db_password").write_text("pw\n", encoding="utf-8")
    (secrets / "api_key").write_text("k\r\n", encoding="utf-8")
    (secrets / "..data").mkdir()
    (secrets / ".hidden").write_text("no", encoding="utf-8")
    (secrets / "subdir").mkdir()
    env = {"SLUICE_ENV_EXPOSE": "REGION", "SLUICE_SECRETS_DIR": str(secrets), "REGION": "eu"}
    cfg = load_config(tmp_path / "none.yaml", environ=env)
    mc = load_mount_config(cfg, environ={**env, "OTHER": "x"})
    assert dict(mc.env) == {"REGION": "eu"}
    assert sorted(mc.secret) == ["api_key", "db_password"]
    assert mc.secret["db_password"].reveal() == "pw"
    assert mc.secret["api_key"].reveal() == "k"


def test_mount_config_missing_env_and_secrets_dir(tmp_path: Path) -> None:
    env = {"SLUICE_SECRETS_DIR": str(tmp_path / "absent")}
    mc = load_mount_config(load_config(tmp_path / "n.yaml", environ=env), environ={})
    assert dict(mc.secret) == {}
    cfg = load_config(tmp_path / "n.yaml", environ={"SLUICE_ENV_EXPOSE": "NOPE"})
    with pytest.raises(ConfigError, match="NOPE"):
        load_mount_config(cfg, environ={})


def test_mount_config_unreadable(tmp_path: Path) -> None:
    afile = tmp_path / "file"
    afile.write_text("x", encoding="utf-8")
    cfg = load_config(tmp_path / "n.yaml", environ={"SLUICE_SECRETS_DIR": str(afile)})
    with pytest.raises(ConfigError, match="cannot read"):
        load_mount_config(cfg, environ={})
    secrets = tmp_path / "s"
    secrets.mkdir()
    (secrets / "bin").write_bytes(b"\xff\xfe")
    cfg = load_config(tmp_path / "n.yaml", environ={"SLUICE_SECRETS_DIR": str(secrets)})
    with pytest.raises(ConfigError, match="bin"):
        load_mount_config(cfg, environ={})


def test_errors() -> None:
    assert str(FsError(errno.ENOENT)) == "ENOENT: No such file or directory"
    assert Deny().errno == errno.EACCES
    assert str(FsError(99999, "odd")) == "99999: odd"


def virtual_ctx(op: str, args: object = None, path: str = "/v") -> Context:
    app = Sluice()
    app.route("/v", virtual=True, handler=VirtualHandler())
    res = app.freeze().resolve(path)
    assert res is not None
    return Context(op, res, config=MountConfig(), args=args)


def test_virtual_defaults() -> None:
    async def go() -> None:
        h = VirtualHandler()
        attr = await h.getattr(virtual_ctx("getattr"))
        assert attr.st_mode & 0o777 == 0o444
        with pytest.raises(FsError):
            await h.getattr(virtual_ctx("getattr", path="/v/deeper"))
        opened = await h.open(virtual_ctx("open", OpenArgs(os.O_RDONLY)))
        assert opened == Opened(data=b"", direct_io=True)
        for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC):
            with pytest.raises(FsError) as e:
                await h.open(virtual_ctx("open", OpenArgs(flags)))
            assert e.value.errno == errno.EROFS
        data = Opened(data=b"hello")
        assert await h.read(virtual_ctx("read", ReadArgs(1, 3)), data) == b"ell"
        ctx = virtual_ctx("x")
        for name in ("setattr", "mkdir", "rmdir", "unlink", "symlink", "rename", "create"):
            with pytest.raises(FsError) as e:
                await getattr(h, name)(ctx)
            assert e.value.errno == errno.EROFS, name
        for name in ("setxattr", "removexattr"):
            with pytest.raises(FsError):
                await getattr(h, name)(ctx)
        with pytest.raises(FsError):
            await h.write(ctx, data)
        with pytest.raises(FsError) as e:
            await h.readlink(ctx)
        assert e.value.errno == errno.EINVAL
        with pytest.raises(FsError) as e:
            await h.opendir(ctx)
        assert e.value.errno == errno.ENOTDIR
        with pytest.raises(FsError):
            await h.readdir(ctx, data)
        with pytest.raises(FsError) as e:
            await h.getxattr(ctx)
        assert e.value.errno == errno.ENODATA
        assert await h.listxattr(ctx) == []
        # No-ops: they must not raise (and must not be ENOSYS, which the kernel would remember).
        await h.flush(ctx, data)
        await h.fsync(ctx, data)
        await h.release(data)
        await h.releasedir(data)
        from sluice.context import AccessArgs

        assert await h.access(virtual_ctx("access", AccessArgs(os.R_OK)))
        assert not await h.access(virtual_ctx("access", AccessArgs(os.W_OK)))

    asyncio.run(go())


def test_synthetic_dir() -> None:
    async def go() -> None:
        d = SyntheticDir()
        ctx = virtual_ctx("getattr")
        attr = await d.getattr(ctx)
        assert attr == Attr.directory(0o555, mtime_ns=d.mtime_ns)
        assert await d.opendir(ctx) == Opened()
        assert await d.readdir(ctx, Opened()) == []
        with pytest.raises(FsError) as e:
            await d.open(ctx)
        assert e.value.errno == errno.EISDIR
        with pytest.raises(FsError):
            await d.mkdir(virtual_ctx("mkdir", MkdirArgs(0o755)))

    asyncio.run(go())


def test_context_fields() -> None:
    ctx = virtual_ctx("getattr")
    assert ctx.op == "getattr"
    assert str(ctx.path) == "/v"
    assert ctx.virtual and ctx.real_path is None
    assert ctx.route.full_pattern == "/v"
    assert ctx.caller.pid == os.getpid()
    assert ctx.inode == 0 and ctx.timestamp > 0
    assert ctx.config == MountConfig()
    assert "getattr /v" in repr(ctx)
    assert not ctx.responded
    ctx.respond(1)
    assert ctx.responded and ctx.response == 1


def test_use_adds_global_middleware_first() -> None:
    async def g(ctx: Context) -> None:
        return None

    async def r(ctx: Context) -> None:
        return None

    app = Sluice()
    app.use(g)
    app.route("/a", backend="/a", middlewares=[r])
    res = app.freeze().resolve("/a/x")
    assert res is not None
    assert [repr(m).rsplit(".", 1)[-1] for m in res.middlewares] == ["g", "r"]
    assert app.root.full_pattern == "/"
