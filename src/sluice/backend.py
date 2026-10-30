"""Passthrough to the backing filesystem.

Every syscall runs in a bounded worker thread pool (`io.threads`), so a slow disk never blocks
the event loop. The daemon makes every call with its own uid, whoever the caller is.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import functools
import os
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sluice.context import Attr, DirEntry, Opened
from sluice.errors import FsError

if TYPE_CHECKING:
    from sluice.context import Context

_OPEN_IGNORED = os.O_CREAT | os.O_EXCL | os.O_NOCTTY
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
_AT_FDCWD = -100


def _real(ctx: Context) -> Path:
    if ctx.real_path is None:
        raise FsError(errno.EIO, "no real path")
    return ctx.real_path


def _rename(old: Path, new: Path, flags: int) -> None:
    if not flags:
        os.rename(old, new)
        return
    if _renameat2 is None:  # pragma: no cover - glibc older than 2.28
        raise FsError(errno.EINVAL, "rename flags need renameat2")
    if _renameat2(_AT_FDCWD, os.fsencode(old), _AT_FDCWD, os.fsencode(new), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _setattr(path: Path, args: Any) -> os.stat_result:
    st = os.lstat(path)
    if args.mode is not None and not stat.S_ISLNK(st.st_mode):
        os.chmod(path, stat.S_IMODE(args.mode))
    if args.uid is not None or args.gid is not None:
        uid = -1 if args.uid is None else args.uid
        gid = -1 if args.gid is None else args.gid
        os.chown(path, uid, gid, follow_symlinks=False)
    if args.size is not None:
        os.truncate(path, args.size)
    if args.atime_ns is not None or args.mtime_ns is not None:
        atime = st.st_atime_ns if args.atime_ns is None else args.atime_ns
        mtime = st.st_mtime_ns if args.mtime_ns is None else args.mtime_ns
        os.utime(path, ns=(atime, mtime), follow_symlinks=False)
    return os.lstat(path)


def _mkdir(path: Path, mode: int) -> os.stat_result:
    os.mkdir(path, mode)
    # The kernel already applied the caller's umask; undo the daemon's.
    os.chmod(path, stat.S_IMODE(mode))
    return os.lstat(path)


def _create(path: Path, flags: int, mode: int) -> tuple[int, os.stat_result]:
    fd = os.open(path, flags | os.O_CREAT, mode)
    try:
        os.fchmod(fd, stat.S_IMODE(mode))
        return fd, os.fstat(fd)
    except OSError:
        os.close(fd)
        raise


def _symlink(target: str, path: Path) -> os.stat_result:
    os.symlink(target, path)
    return os.lstat(path)


def _opendir(path: Path) -> None:
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory")


def _scandir(path: Path) -> list[DirEntry]:
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # Removed while listing
            entries.append(DirEntry(entry.name, Attr.from_stat(st)))
    return entries


def _flush(fd: int) -> None:
    # close() of a duplicate gives the backend the close-time semantics (NFS, quotas) of
    # the caller's close without closing the fd that later reads still use.
    os.close(os.dup(fd))


class Passthrough:
    """The operation for backed routes, against `ctx.real_path`."""

    def __init__(self, threads: int = 8) -> None:
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="sluice-io")

    async def run[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(fn, *args, **kwargs))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    async def lstat(self, path: Path) -> Attr:
        return Attr.from_stat(await self.run(os.lstat, path))

    async def getattr(self, ctx: Context) -> Attr:
        return await self.lstat(_real(ctx))

    async def setattr(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.run(_setattr, _real(ctx), ctx.args))

    async def readlink(self, ctx: Context) -> str:
        return await self.run(os.readlink, _real(ctx))

    async def mkdir(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.run(_mkdir, _real(ctx), ctx.args.mode))

    async def rmdir(self, ctx: Context) -> None:
        await self.run(os.rmdir, _real(ctx))

    async def unlink(self, ctx: Context) -> None:
        await self.run(os.unlink, _real(ctx))

    async def symlink(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.run(_symlink, ctx.args.target, _real(ctx)))

    async def rename(self, ctx: Context) -> None:
        target = ctx.args.target_real_path
        if target is None:
            raise FsError(errno.EXDEV)
        await self.run(_rename, _real(ctx), target, ctx.args.flags)

    async def open(self, ctx: Context) -> Opened:
        fd = await self.run(os.open, _real(ctx), ctx.args.flags & ~_OPEN_IGNORED)
        return Opened(fd=fd, direct_io=ctx.resolution.direct_io)

    async def create(self, ctx: Context) -> tuple[Opened, Attr]:
        fd, st = await self.run(_create, _real(ctx), ctx.args.flags, ctx.args.mode)
        return Opened(fd=fd, direct_io=ctx.resolution.direct_io), Attr.from_stat(st)

    async def read(self, ctx: Context, handle: Opened) -> bytes:
        return await self.run(os.pread, handle.fd, ctx.args.size, ctx.args.offset)

    async def write(self, ctx: Context, handle: Opened) -> int:
        return await self.run(os.pwrite, handle.fd, ctx.args.data, ctx.args.offset)

    async def flush(self, ctx: Context, handle: Opened) -> None:
        if handle.fd is not None:
            await self.run(_flush, handle.fd)

    async def fsync(self, ctx: Context, handle: Opened) -> None:
        await self.run(os.fdatasync if ctx.args.datasync else os.fsync, handle.fd)

    async def release(self, handle: Opened) -> None:
        if handle.fd is not None:
            await self.run(os.close, handle.fd)

    async def opendir(self, ctx: Context) -> Opened:
        await self.run(_opendir, _real(ctx))
        return Opened()

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        return await self.run(_scandir, _real(ctx))

    async def releasedir(self, handle: Opened) -> None:
        return None

    async def access(self, ctx: Context) -> bool:
        return await self.run(os.access, _real(ctx), ctx.args.mode)

    async def getxattr(self, ctx: Context) -> bytes:
        return await self.run(os.getxattr, _real(ctx), ctx.args.name, follow_symlinks=False)

    async def setxattr(self, ctx: Context) -> None:
        args = ctx.args
        await self.run(os.setxattr, _real(ctx), args.name, args.value, follow_symlinks=False)

    async def listxattr(self, ctx: Context) -> list[str]:
        return await self.run(os.listxattr, _real(ctx), follow_symlinks=False)

    async def removexattr(self, ctx: Context) -> None:
        await self.run(os.removexattr, _real(ctx), ctx.args.name, follow_symlinks=False)
