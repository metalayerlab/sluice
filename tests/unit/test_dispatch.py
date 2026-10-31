"""The dispatcher end to end, against a temporary directory: everything but the kernel."""

import asyncio
import errno
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from sluice import (
    Context,
    Deny,
    FsError,
    Middleware,
    MountConfig,
    Opened,
    Secret,
    Sluice,
    VirtualHandler,
    access_log,
)
from sluice.backend import Passthrough
from sluice.context import Caller, SetattrArgs
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE


class Status(VirtualHandler):
    async def render(self, ctx: Context) -> bytes:
        return f"user={ctx.params['user']}\n".encode()


class ConfigTemplate(VirtualHandler):
    async def render(self, ctx: Context) -> bytes:
        password = ctx.config.secret["db_password"].reveal()
        return f"project: {ctx.params['project']}\npassword: {password}\n".encode()


class Redact(Middleware):
    ops = frozenset({"read"})

    async def post(self, ctx: Context, result: Any) -> Any:
        return result.replace(b"SECRET", b"******")


class AdminOnly(Middleware):
    async def pre(self, ctx: Context) -> None:
        if ctx.params["user"] != "admin":
            raise Deny("admins only")


@pytest.fixture
def backing(tmp_path: Path) -> Path:
    root = tmp_path / "ext4"
    (root / "alice" / "private").mkdir(parents=True)
    (root / "alice" / "notes.txt").write_text("hello SECRET world", encoding="utf-8")
    (root / "alice" / "private" / "p.txt").write_text("p", encoding="utf-8")
    (root / "alice" / "status").write_text("hidden by the route", encoding="utf-8")
    (root / "admin").mkdir()
    return root


@pytest.fixture
def dispatcher(backing: Path) -> Iterator[Dispatcher]:
    async def token(ctx: Context, raw: Any) -> Any:
        if raw == "bad":
            raise Deny("bad token")
        return {"raw": raw}

    app = Sluice()
    app.use(access_log())
    app.param("token", token, secret=True)
    user = app.route("/fuse/{user}/{token}", backend=f"{backing}/{{user}}")
    user.route("private", middlewares=[AdminOnly()])
    user.route("status", virtual=True, handler=Status())
    app.route("/fuse/{project}/config.yaml", virtual=True, handler=ConfigTemplate())
    app.route("/redacted/{user}", backend=f"{backing}/{{user}}", middlewares=[Redact()])
    config = MountConfig(secret={"db_password": Secret("pw")})
    io = Passthrough(2)
    yield Dispatcher(app.freeze(config), io)
    io.shutdown()


def run[T](coro: Awaitable[T]) -> T:
    async def wrap() -> T:
        return await coro

    return asyncio.run(wrap())


async def walk(d: Dispatcher, path: str) -> int:
    inode = ROOT_INODE
    for name in PurePosixPath(path).parts[1:]:
        inode = (await d.lookup(inode, name)).inode
    return inode


async def cat(d: Dispatcher, path: str, chunk_size: int = 4) -> bytes:
    inode = await walk(d, path)
    info = await d.open(inode, os.O_RDONLY)
    data = b""
    while chunk := await d.read(info.fh, len(data), chunk_size):
        data += chunk
    await d.release(info.fh)
    return data


def errno_of(fn: Callable[[], Awaitable[Any]]) -> int:
    with pytest.raises(FsError) as e:
        run(fn())
    return e.value.errno


def test_read_through_inherited_backend(dispatcher: Dispatcher) -> None:
    assert run(cat(dispatcher, "/fuse/alice/T0k/notes.txt")) == b"hello SECRET world"
    assert len(dispatcher.handles) == 0


def test_child_middleware_gates(dispatcher: Dispatcher) -> None:
    assert errno_of(lambda: walk(dispatcher, "/fuse/alice/T0k/private")) == errno.EACCES
    assert run(walk(dispatcher, "/fuse/admin/T0k")) > ROOT_INODE


def test_param_callback_denies(dispatcher: Dispatcher) -> None:
    assert errno_of(lambda: walk(dispatcher, "/fuse/alice/bad")) == errno.EACCES


def test_virtual_status_node(dispatcher: Dispatcher) -> None:
    assert run(cat(dispatcher, "/fuse/alice/T0k/status")) == b"user=alice\n"


def test_templated_secret(dispatcher: Dispatcher) -> None:
    data = run(cat(dispatcher, "/fuse/proj/config.yaml"))
    assert data == b"project: proj\npassword: pw\n"


def test_redaction_post_hook(dispatcher: Dispatcher) -> None:
    # One read: rewriting across read boundaries is out of scope in phase 1.
    data = run(cat(dispatcher, "/redacted/alice/notes.txt", chunk_size=4096))
    assert data == b"hello ****** world"


def test_open_virtual_sets_direct_io(dispatcher: Dispatcher) -> None:
    async def go() -> bool:
        info = await dispatcher.open(await walk(dispatcher, "/fuse/alice/T/status"), os.O_RDONLY)
        await dispatcher.release(info.fh)
        return info.direct_io

    assert run(go())


def test_readdir_lists_literal_children_and_merges(dispatcher: Dispatcher) -> None:
    async def ls(path: str) -> dict[str, int]:
        fh = await dispatcher.opendir(await walk(dispatcher, path))
        listed = await dispatcher.readdir(fh)
        rest = await dispatcher.readdir(fh, 1)
        assert [e.name for e in rest] == [e.name for e in listed[1:]]
        for e in listed:
            dispatcher.readdir_ref(e.inode)
        await dispatcher.releasedir(fh)
        return {e.name: e.attr.st_mode for e in listed}

    assert set(run(ls("/"))) == {"fuse", "redacted"}
    assert run(ls("/fuse")) == {}
    assert set(run(ls("/fuse/alice"))) == {"config.yaml"}
    listing = run(ls("/fuse/alice/T0k"))
    assert set(listing) == {"notes.txt", "private", "status"}
    assert stat.S_ISREG(listing["status"])  # the virtual route, not the real file
    assert stat.S_ISDIR(listing["private"])


def test_synthetic_dirs_are_read_only(dispatcher: Dispatcher) -> None:
    async def go() -> None:
        await dispatcher.mkdir(await walk(dispatcher, "/fuse"), "new", 0o755)

    assert errno_of(go) == errno.EROFS
    assert errno_of(lambda: dispatcher.mkdir(ROOT_INODE, "new", 0o755)) == errno.ENOENT
    attr = run(dispatcher.getattr(ROOT_INODE)).attr
    assert stat.S_ISDIR(attr.st_mode) and attr.st_mode & 0o777 == 0o555
    assert errno_of(lambda: dispatcher.open(ROOT_INODE, os.O_RDONLY)) == errno.EISDIR


def test_unmatched_is_enoent(dispatcher: Dispatcher) -> None:
    assert errno_of(lambda: dispatcher.lookup(ROOT_INODE, "nope")) == errno.ENOENT


def test_write_create_rename_unlink(dispatcher: Dispatcher, backing: Path) -> None:
    async def go() -> None:
        d = dispatcher
        home = await walk(d, "/fuse/alice/T0k")
        info, entry = await d.create(home, "new.txt", 0o640, os.O_WRONLY)
        assert await d.write(info.fh, 0, b"abc") == 3
        await d.flush(info.fh)
        await d.fsync(info.fh, True)
        await d.release(info.fh)
        assert (backing / "alice" / "new.txt").read_bytes() == b"abc"
        assert stat.S_IMODE((backing / "alice" / "new.txt").stat().st_mode) == 0o640

        await d.rename(home, "new.txt", home, "moved.txt")
        assert (backing / "alice" / "moved.txt").exists()
        moved = await d.getattr(entry.inode)  # same inode follows the rename
        assert moved.attr.st_size == 3

        await d.unlink(home, "moved.txt")
        assert not (backing / "alice" / "moved.txt").exists()

        sub = await d.mkdir(home, "dir", 0o750)
        assert stat.S_IMODE((backing / "alice" / "dir").stat().st_mode) == 0o750
        await d.rmdir(home, "dir")
        assert not (backing / "alice" / "dir").exists()
        with pytest.raises(FsError):
            await d.getattr(sub.inode)

    run(go())


def test_rename_into_private_is_gated(dispatcher: Dispatcher) -> None:
    async def go() -> None:
        home = await walk(dispatcher, "/fuse/alice/T0k")
        await dispatcher.rename(home, "notes.txt", home, "private")

    assert errno_of(go) == errno.EACCES


def test_rename_across_backends_is_exdev(dispatcher: Dispatcher) -> None:
    async def go() -> None:
        alice = await walk(dispatcher, "/fuse/alice/T0k")
        admin = await walk(dispatcher, "/fuse/admin/T0k")
        await dispatcher.rename(alice, "notes.txt", admin, "notes.txt")

    assert errno_of(go) == errno.EXDEV


def test_rename_noreplace(dispatcher: Dispatcher, backing: Path) -> None:
    (backing / "alice" / "other.txt").write_text("o", encoding="utf-8")

    async def go() -> None:
        home = await walk(dispatcher, "/fuse/alice/T0k")
        await dispatcher.rename(home, "notes.txt", home, "other.txt", flags=1)

    assert errno_of(go) == errno.EEXIST


def test_symlink_readlink_setattr(dispatcher: Dispatcher, backing: Path) -> None:
    async def go() -> None:
        d = dispatcher
        home = await walk(d, "/fuse/alice/T0k")
        link = await d.symlink(home, "ln", "notes.txt")
        assert await d.readlink(link.inode) == "notes.txt"
        notes = await d.lookup(home, "notes.txt")
        attr = await d.setattr(notes.inode, SetattrArgs(mode=0o600, size=5, mtime_ns=10**18))
        assert attr.attr.st_size == 5
        assert attr.attr.st_mtime_ns == 10**18
        assert stat.S_IMODE(attr.attr.st_mode) == 0o600
        uid = os.getuid()
        await d.setattr(notes.inode, SetattrArgs(uid=uid, atime_ns=10**18))
        await d.setattr(link.inode, SetattrArgs(mode=0o777))  # not applied to a symlink

    run(go())
    assert (backing / "alice" / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_xattrs_access_statfs(dispatcher: Dispatcher, backing: Path) -> None:
    async def go() -> None:
        d = dispatcher
        notes = await walk(d, "/fuse/alice/T0k/notes.txt")
        try:
            await d.setxattr(notes, "user.tag", b"v")
        except FsError as e:
            if e.errno == errno.ENOTSUP:  # tmpfs without user xattrs
                return
            raise
        assert await d.getxattr(notes, "user.tag") == b"v"
        assert "user.tag" in await d.listxattr(notes)
        await d.removexattr(notes, "user.tag")
        with pytest.raises(FsError):
            await d.getxattr(notes, "user.tag")

    run(go())
    notes = run(walk(dispatcher, "/fuse/alice/T0k/notes.txt"))
    assert run(dispatcher.access(notes, os.R_OK))
    assert run(dispatcher.statfs()).f_namemax == 255


def test_opendir_on_a_file_and_link_mknod(dispatcher: Dispatcher) -> None:
    notes = run(walk(dispatcher, "/fuse/alice/T0k/notes.txt"))
    assert errno_of(lambda: dispatcher.opendir(notes)) == errno.ENOTDIR
    assert errno_of(lambda: dispatcher.link(notes, ROOT_INODE, "x")) == errno.ENOTSUP
    assert errno_of(lambda: dispatcher.mknod(ROOT_INODE, "x", 0, 0)) == errno.ENOTSUP


def test_post_failure_on_open_closes_the_handle(backing: Path) -> None:
    closed: list[object] = []

    class Broken(Middleware):
        ops = frozenset({"open", "opendir"})

        async def post(self, ctx: Context, result: Any) -> Any:
            raise RuntimeError("post broke")

    class Io(Passthrough):
        async def release(self, handle: Any) -> None:
            closed.append(handle.fd)
            await super().release(handle)

        async def releasedir(self, handle: Any) -> None:
            closed.append("dir")
            raise RuntimeError("logged and ignored")

    app = Sluice()
    app.route("/b", backend=str(backing), middlewares=[Broken()])
    io = Io(1)
    d = Dispatcher(app.freeze(), io)
    notes = run(walk(d, "/b/alice/notes.txt"))
    assert errno_of(lambda: d.open(notes, os.O_RDONLY)) == errno.EIO
    alice = run(walk(d, "/b/alice"))
    assert errno_of(lambda: d.opendir(alice)) == errno.EIO
    io.shutdown()
    assert len(closed) == 2 and isinstance(closed[0], int)
    assert len(d.handles) == 0


def test_post_replacing_opened_closes_the_dropped_handle(backing: Path) -> None:
    closed: list[int | None] = []

    class Serve(Middleware):
        ops = frozenset({"open", "create"})

        async def post(self, ctx: Context, result: Any) -> Any:
            if ctx.path.name == "notes.txt":
                return Opened(data=b"served")
            if isinstance(result, tuple):  # create: keep the fd, change a flag
                return replace(result[0], direct_io=True), result[1]
            return result

    class Io(Passthrough):
        async def release(self, handle: Any) -> None:
            closed.append(handle.fd)
            await super().release(handle)

    app = Sluice()
    app.route("/b", backend=str(backing), middlewares=[Serve()])
    io = Io(1)
    d = Dispatcher(app.freeze(), io)

    async def go() -> bytes:
        info = await d.open(await walk(d, "/b/alice/notes.txt"), os.O_RDONLY)
        assert len(closed) == 1 and isinstance(closed[0], int)  # the dropped fd, at once
        await d.release(info.fh)
        assert closed[1:] == [None]  # the replacement
        made, _ = await d.create(await walk(d, "/b/alice"), "new.txt", 0o644, os.O_RDWR)
        assert len(closed) == 2  # the replacement shares the fd: still open
        await d.write(made.fh, 0, b"kept")
        await d.release(made.fh)
        return (backing / "alice" / "new.txt").read_bytes()

    assert run(go()) == b"kept"
    io.shutdown()
    assert len(closed) == 3 and isinstance(closed[2], int)


def test_post_replacing_opened_without_fd_or_with_junk(backing: Path) -> None:
    released: list[int | None] = []

    class Tweak(Middleware):
        ops = frozenset({"open"})

        async def post(self, ctx: Context, result: Any) -> Any:
            return None if ctx.path.name == "notes.txt" else replace(result, keep_cache=True)

    class Counted(Status):
        async def release(self, handle: Opened) -> None:
            released.append(handle.fd)

    class Io(Passthrough):
        async def release(self, handle: Any) -> None:
            released.append(handle.fd)
            await super().release(handle)

    app = Sluice()
    app.route("/b", backend=str(backing), middlewares=[Tweak()])
    app.route("/v/{user}", virtual=True, handler=Counted(), middlewares=[Tweak()])
    io = Io(1)
    d = Dispatcher(app.freeze(), io)
    notes = run(walk(d, "/b/alice/notes.txt"))
    assert errno_of(lambda: d.open(notes, os.O_RDONLY)) == errno.EIO
    assert len(released) == 1 and isinstance(released[0], int)  # the fd is closed

    async def go() -> None:
        info = await d.open(await walk(d, "/v/alice"), os.O_RDONLY)
        assert info.keep_cache and len(released) == 1  # the copy isn't released early
        await d.release(info.fh)

    run(go())
    io.shutdown()
    assert released[1:] == [None]  # released once, by the kernel


def test_forget_drops_inodes(dispatcher: Dispatcher) -> None:
    inode = run(walk(dispatcher, "/fuse"))
    before = len(dispatcher.inodes)
    dispatcher.forget([(inode, 1)])
    assert len(dispatcher.inodes) == before - 1
    assert errno_of(lambda: dispatcher.getattr(inode)) == errno.ENOENT


def test_handle_survives_rename(dispatcher: Dispatcher, backing: Path) -> None:
    async def go() -> bytes:
        d = dispatcher
        home = await walk(d, "/fuse/alice/T0k")
        info = await d.open((await d.lookup(home, "notes.txt")).inode, os.O_RDONLY)
        await d.rename(home, "notes.txt", home, "renamed.txt")
        data = await d.read(info.fh, 0, 5)
        await d.release(info.fh)
        return data

    assert run(go()) == b"hello"


def test_open_file_outlives_unlink(dispatcher: Dispatcher) -> None:
    async def go() -> tuple[int, int, bytes, int]:
        d = dispatcher
        home = await walk(d, "/fuse/alice/T0k")
        inode = (await d.lookup(home, "notes.txt")).inode
        info = await d.open(inode, os.O_RDWR)
        await d.unlink(home, "notes.txt")
        size = (await d.getattr(inode)).attr.st_size
        truncated = (await d.setattr(inode, SetattrArgs(size=5), fh=info.fh)).attr.st_size
        data = await d.read(info.fh, 0, 100)
        await d.release(info.fh)
        return size, truncated, data, await errno_in(d.getattr(inode))

    size, truncated, data, closed = run(go())
    assert (size, truncated, data) == (len("hello SECRET world"), 5, b"hello")
    assert closed == errno.ENOENT  # once closed, nothing names it


def test_open_file_outlives_rename_over(dispatcher: Dispatcher, backing: Path) -> None:
    (backing / "alice" / "new.txt").write_text("replacement", encoding="utf-8")

    async def go() -> tuple[int, int]:
        d = dispatcher
        home = await walk(d, "/fuse/alice/T0k")
        inode = (await d.lookup(home, "notes.txt")).inode
        info = await d.open(inode, os.O_RDONLY)
        await d.rename(home, "new.txt", home, "notes.txt")
        old = (await d.getattr(inode)).attr.st_size
        new = (await d.lookup(home, "notes.txt")).attr.st_size
        await d.release(info.fh)
        return old, new

    assert run(go()) == (len("hello SECRET world"), len("replacement"))


def test_open_virtual_file_outlives_unlink() -> None:
    class Removable(VirtualHandler):
        async def unlink(self, ctx: Context) -> None:
            return None

    app = Sluice()
    app.route("/v", virtual=True, handler=Removable())
    io = Passthrough(1)
    d = Dispatcher(app.freeze(), io)

    async def go() -> tuple[int, int]:
        inode = (await d.lookup(ROOT_INODE, "v")).inode
        info = await d.open(inode, os.O_RDONLY)
        await d.unlink(ROOT_INODE, "v")
        mode = (await d.getattr(inode)).attr.st_mode
        denied = await errno_in(d.setattr(inode, SetattrArgs(size=0)))
        await d.release(info.fh)
        return mode, denied

    try:
        mode, denied = run(go())
    finally:
        io.shutdown()
    assert stat.S_ISREG(mode)
    assert denied == errno.EROFS


def test_orphan_call_runs_as_the_requester(backing: Path) -> None:
    seen: list[int] = []

    class Spy(Middleware):
        ops = frozenset({"getattr"})

        async def pre(self, ctx: Context) -> None:
            assert ctx.caller is not None
            seen.append(ctx.caller.uid)

    app = Sluice()
    app.use(Spy())
    app.route("/b", backend=str(backing / "alice"))
    io = Passthrough(1)
    d = Dispatcher(app.freeze(), io)
    opener, requester = Caller(1000, 1000, 1), Caller(2000, 2000, 2)

    async def go() -> None:
        home = (await d.lookup(ROOT_INODE, "b")).inode
        inode = (await d.lookup(home, "notes.txt")).inode
        info = await d.open(inode, os.O_RDONLY, opener)
        await d.unlink(home, "notes.txt")
        seen.clear()
        await d.getattr(inode, requester)
        await d.release(info.fh)

    try:
        run(go())
    finally:
        io.shutdown()
    assert seen == [2000]


async def errno_in(coro: Awaitable[Any]) -> int:
    with pytest.raises(FsError) as e:
        await coro
    return e.value.errno


def test_bad_handle_is_ebadf(dispatcher: Dispatcher) -> None:
    assert errno_of(lambda: dispatcher.read(999, 0, 1)) == errno.EBADF
    run(dispatcher.release(999))  # unknown handles are ignored
    run(dispatcher.releasedir(999))


def test_access_log_masks_tokens(dispatcher: Dispatcher, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sluice.access"):
        run(cat(dispatcher, "/fuse/alice/T0k/notes.txt"))
    assert "/fuse/alice/********/notes.txt" in caplog.text
    assert "T0k" not in caplog.text
