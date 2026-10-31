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
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sluice.context import Attr, DirEntry, Opened
from sluice.errors import FsError

if TYPE_CHECKING:
    from sluice.context import Context
    from sluice.routing import Resolution

_OPEN_IGNORED = os.O_CREAT | os.O_EXCL | os.O_NOCTTY
_WALK = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)

type At = tuple[int, str]  # A directory fd and a name in it


def _where(res: Resolution) -> tuple[Path, Path]:
    if res.real_path is None or res.backend_prefix is None:
        raise FsError(errno.EIO, "no real path")
    return res.backend_prefix, res.real_path


def _fd(handle: Opened) -> int:
    if handle.fd is None:
        raise FsError(errno.ENOENT, "no backend fd")
    return handle.fd


@contextmanager
def _beneath(prefix: Path, real: Path) -> Iterator[At]:
    """`real`'s parent directory and final name, reached from `prefix` without following links.

    The router checks `real` stays under the template's literal `prefix` only as a string. A
    symlink swapped in for a directory below it would still take the daemon's own syscalls
    outside, so every component below `prefix` is opened `O_NOFOLLOW`.
    `prefix` itself is the admin's and may be a link. The final component is left to each op.
    """
    try:
        names = real.relative_to(prefix).parts
    except ValueError:
        raise FsError(errno.EACCES, "path escapes its backend") from None
    top = prefix
    if not names:
        # The backend itself: its own name is the final component, as in lstat(prefix).
        top, names = prefix.parent, (prefix.name or ".",)
    fd = os.open(top, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in names[:-1]:
            parent = fd
            fd = os.open(name, _WALK, dir_fd=parent)
            os.close(parent)
        yield fd, names[-1]
    finally:
        os.close(fd)


def _at[T](fn: Callable[..., T]) -> Callable[..., T]:
    """Run `fn(at, ...)` on `real`, resolved beneath `prefix`."""

    def wrapper(prefix: Path, real: Path, *args: Any) -> T:
        with _beneath(prefix, real) as at:
            return fn(at, *args)

    return wrapper


def _lstat(at: At) -> os.stat_result:
    return os.stat(at[1], dir_fd=at[0], follow_symlinks=False)


def _chmod(at: At, mode: int) -> None:
    try:
        os.chmod(at[1], mode, dir_fd=at[0], follow_symlinks=False)
    except ValueError:  # Python's message for fchmodat's EOPNOTSUPP: the name became a link
        raise FsError(errno.EOPNOTSUPP, "chmod of a symlink") from None


def _truncate(at: At, st: os.stat_result, size: int) -> None:
    # truncate(2) has no *at form: open the file itself, never a device or a link to one.
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(errno.EISDIR, "is a directory")
    if not stat.S_ISREG(st.st_mode):
        raise FsError(errno.EINVAL, "not a regular file")
    fd = os.open(at[1], os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=at[0])
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def _setattr(target: At | int, args: Any) -> os.stat_result:
    """Apply `args` to a name, without following a symlink, or to an open fd."""
    # os functions reject dir_fd and follow_symlinks with an fd; an fd is never a symlink.
    path: str | int
    nofollow: dict[str, Any]
    if isinstance(target, int):
        path, nofollow = target, {}
    else:
        path, nofollow = target[1], {"dir_fd": target[0], "follow_symlinks": False}
    st = os.stat(path, **nofollow)
    if args.mode is not None and not stat.S_ISLNK(st.st_mode):
        if isinstance(target, int):
            os.fchmod(target, stat.S_IMODE(args.mode))
        else:
            _chmod(target, stat.S_IMODE(args.mode))
    if args.uid is not None or args.gid is not None:
        uid = -1 if args.uid is None else args.uid
        gid = -1 if args.gid is None else args.gid
        os.chown(path, uid, gid, **nofollow)
    if args.size is not None:
        if isinstance(target, int):
            os.ftruncate(target, args.size)
        else:
            _truncate(target, st, args.size)
    if args.atime_ns is not None or args.mtime_ns is not None:
        atime = st.st_atime_ns if args.atime_ns is None else args.atime_ns
        mtime = st.st_mtime_ns if args.mtime_ns is None else args.mtime_ns
        os.utime(path, ns=(atime, mtime), **nofollow)
    return os.stat(path, **nofollow)


def _mkdir(at: At, mode: int) -> os.stat_result:
    os.mkdir(at[1], mode, dir_fd=at[0])
    # The kernel already applied the caller's umask; undo the daemon's.
    _chmod(at, stat.S_IMODE(mode))
    return _lstat(at)


def _open(at: At, flags: int, mode: int = 0o777) -> int:
    # The kernel follows a symlink itself before it opens: a link here was swapped in.
    return os.open(at[1], flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=at[0])


def _create(at: At, flags: int, mode: int) -> tuple[int, os.stat_result]:
    fd = _open(at, flags | os.O_CREAT, mode)
    try:
        os.fchmod(fd, stat.S_IMODE(mode))
        return fd, os.fstat(fd)
    except OSError:
        os.close(fd)
        raise


def _symlink(at: At, target: str) -> os.stat_result:
    os.symlink(target, at[1], dir_fd=at[0])
    return _lstat(at)


def _readlink(at: At) -> str:
    return os.readlink(at[1], dir_fd=at[0])


def _rmdir(at: At) -> None:
    os.rmdir(at[1], dir_fd=at[0])


def _unlink(at: At) -> None:
    os.unlink(at[1], dir_fd=at[0])


def _access(at: At, mode: int) -> bool:
    return os.access(at[1], mode, dir_fd=at[0], follow_symlinks=False)


def _opendir(at: At) -> None:
    if not stat.S_ISDIR(_lstat(at).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory")


def _scandir(at: At) -> list[DirEntry]:
    entries = []
    fd = _open(at, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Removed while listing
                entries.append(DirEntry(entry.name, Attr.from_stat(st)))
    finally:
        os.close(fd)
    return entries


def _xattr[T](fn: Callable[..., T]) -> Callable[..., T]:
    """Run an `os.*xattr` on a name: there's no *at form, so through the parent's /proc fd link."""

    def wrapper(at: At, *args: Any) -> T:
        return fn(f"/proc/self/fd/{at[0]}/{at[1]}", *args, follow_symlinks=False)

    return wrapper


def _rename(prefix: Path, old: Path, new: Path, flags: int) -> None:
    with _beneath(prefix, old) as (src, src_name), _beneath(prefix, new) as (dst, dst_name):
        if not flags:
            os.rename(src_name, dst_name, src_dir_fd=src, dst_dir_fd=dst)
            return
        if _renameat2 is None:  # pragma: no cover - glibc older than 2.28
            raise FsError(errno.EINVAL, "rename flags need renameat2")
        if _renameat2(src, os.fsencode(src_name), dst, os.fsencode(dst_name), flags) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))


def _flush(fd: int) -> None:
    # close() of a duplicate gives the backend the close-time semantics (NFS, quotas) of
    # the caller's close without closing the fd that later reads still use.
    os.close(os.dup(fd))


class Passthrough:
    """The operation for backed routes, against `ctx.real_path`.

    Path operations never follow a symlink below the backend's literal prefix: see `_beneath`.
    """

    def __init__(self, threads: int = 8) -> None:
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="sluice-io")

    async def run[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(fn, *args, **kwargs))

    async def at[T](self, fn: Callable[..., T], res: Resolution, *args: Any) -> T:
        """Run `fn(at, *args)` in the pool, on `res.real_path` resolved beneath its prefix."""
        return await self.run(_at(fn), *_where(res), *args)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    async def lstat(self, res: Resolution) -> Attr:
        return Attr.from_stat(await self.at(_lstat, res))

    async def getattr(self, ctx: Context) -> Attr:
        return await self.lstat(ctx.resolution)

    async def setattr(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.at(_setattr, ctx.resolution, ctx.args))

    async def fgetattr(self, ctx: Context, handle: Opened) -> Attr:
        """`getattr` through an open fd: the file it opened, whatever its path now names."""
        return Attr.from_stat(await self.run(os.fstat, _fd(handle)))

    async def fsetattr(self, ctx: Context, handle: Opened) -> Attr:
        return Attr.from_stat(await self.run(_setattr, _fd(handle), ctx.args))

    async def readlink(self, ctx: Context) -> str:
        return await self.at(_readlink, ctx.resolution)

    async def mkdir(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.at(_mkdir, ctx.resolution, ctx.args.mode))

    async def rmdir(self, ctx: Context) -> None:
        await self.at(_rmdir, ctx.resolution)

    async def unlink(self, ctx: Context) -> None:
        await self.at(_unlink, ctx.resolution)

    async def symlink(self, ctx: Context) -> Attr:
        return Attr.from_stat(await self.at(_symlink, ctx.resolution, ctx.args.target))

    async def rename(self, ctx: Context) -> None:
        target = ctx.args.target_real_path
        if target is None:
            raise FsError(errno.EXDEV)
        # The target shares the source's backend root (the dispatcher checks), so its prefix.
        await self.run(_rename, *_where(ctx.resolution), target, ctx.args.flags)

    async def open(self, ctx: Context) -> Opened:
        fd = await self.at(_open, ctx.resolution, ctx.args.flags & ~_OPEN_IGNORED)
        return Opened(fd=fd, direct_io=ctx.resolution.direct_io)

    async def create(self, ctx: Context) -> tuple[Opened, Attr]:
        fd, st = await self.at(_create, ctx.resolution, ctx.args.flags, ctx.args.mode)
        return Opened(fd=fd, direct_io=ctx.resolution.direct_io), Attr.from_stat(st)

    # A hook may replace `open`'s result with an fd-less one (virtual content, say): serve
    # it as `VirtualHandler` would, a read-only snapshot.
    async def read(self, ctx: Context, handle: Opened) -> bytes:
        if handle.fd is None:
            offset: int = ctx.args.offset
            return handle.data[offset : offset + ctx.args.size]
        return await self.run(os.pread, handle.fd, ctx.args.size, ctx.args.offset)

    async def write(self, ctx: Context, handle: Opened) -> int:
        if handle.fd is None:
            raise FsError(errno.EROFS, "a hook replaced the open fd")
        return await self.run(os.pwrite, handle.fd, ctx.args.data, ctx.args.offset)

    async def flush(self, ctx: Context, handle: Opened) -> None:
        if handle.fd is not None:
            await self.run(_flush, handle.fd)

    async def fsync(self, ctx: Context, handle: Opened) -> None:
        if handle.fd is not None:
            await self.run(os.fdatasync if ctx.args.datasync else os.fsync, handle.fd)

    async def release(self, handle: Opened) -> None:
        if handle.fd is not None:
            await self.run(os.close, handle.fd)

    async def opendir(self, ctx: Context) -> Opened:
        await self.at(_opendir, ctx.resolution)
        return Opened()

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        return await self.at(_scandir, ctx.resolution)

    async def releasedir(self, handle: Opened) -> None:
        return None

    async def access(self, ctx: Context) -> bool:
        return await self.at(_access, ctx.resolution, ctx.args.mode)

    async def getxattr(self, ctx: Context) -> bytes:
        return await self.at(_xattr(os.getxattr), ctx.resolution, ctx.args.name)

    async def setxattr(self, ctx: Context) -> None:
        args = ctx.args
        await self.at(_xattr(os.setxattr), ctx.resolution, args.name, args.value)

    async def listxattr(self, ctx: Context) -> list[str]:
        return await self.at(_xattr(os.listxattr), ctx.resolution)

    async def removexattr(self, ctx: Context) -> None:
        await self.at(_xattr(os.removexattr), ctx.resolution, ctx.args.name)
