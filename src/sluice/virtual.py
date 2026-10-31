"""Virtual routes: content with no backing file, from a `VirtualHandler`.

The defaults make a read-only regular file whose content is `render(ctx)`, run once per `open`.
Write ops fail with `EROFS`. Nothing returns `ENOSYS`: the kernel remembers `ENOSYS` for some ops
(xattrs, `access`, `flush`, `fsync`, `opendir`) for the whole mount, backed routes included.
"""

from __future__ import annotations

import errno
import os
import time
from typing import TYPE_CHECKING

from sluice.context import Attr, DirEntry, Opened
from sluice.errors import FsError

if TYPE_CHECKING:
    from sluice.context import Context


def _erofs() -> FsError:
    return FsError(errno.EROFS, "read-only virtual path")


class VirtualHandler:
    """Implements every operation for a `virtual=True` route. Override what you need."""

    async def render(self, ctx: Context) -> bytes:
        """The file's content. Runs once at `open`; reads see that snapshot."""
        return b""

    async def getattr(self, ctx: Context) -> Attr:
        if ctx.resolution.tail:
            raise FsError(errno.ENOENT)
        # The size isn't known until render() runs at open; files open with direct_io, so the
        # kernel reads to EOF regardless.
        return Attr.file(0o444, mtime_ns=int(ctx.timestamp * 1e9))

    async def setattr(self, ctx: Context) -> Attr:
        raise _erofs()

    async def readlink(self, ctx: Context) -> str:
        raise FsError(errno.EINVAL, "not a symbolic link")

    async def mkdir(self, ctx: Context) -> Attr:
        raise _erofs()

    async def rmdir(self, ctx: Context) -> None:
        raise _erofs()

    async def unlink(self, ctx: Context) -> None:
        raise _erofs()

    async def symlink(self, ctx: Context) -> Attr:
        raise _erofs()

    async def rename(self, ctx: Context) -> None:
        raise _erofs()

    async def open(self, ctx: Context) -> Opened:
        flags: int = ctx.args.flags
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & os.O_TRUNC:
            raise _erofs()
        return Opened(data=await self.render(ctx), direct_io=True)

    async def create(self, ctx: Context) -> tuple[Opened, Attr]:
        raise _erofs()

    async def read(self, ctx: Context, handle: Opened) -> bytes:
        offset: int = ctx.args.offset
        return handle.data[offset : offset + ctx.args.size]

    async def write(self, ctx: Context, handle: Opened) -> int:
        raise _erofs()

    async def flush(self, ctx: Context, handle: Opened) -> None:
        return None

    async def fsync(self, ctx: Context, handle: Opened) -> None:
        return None

    async def release(self, handle: Opened) -> None:
        """Bookkeeping when the file is closed. Runs without middleware and can't fail the call."""
        return None

    async def opendir(self, ctx: Context) -> Opened:
        raise FsError(errno.ENOTDIR)

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        raise FsError(errno.ENOTDIR)

    async def releasedir(self, handle: Opened) -> None:
        return None

    async def access(self, ctx: Context) -> bool:
        return not ctx.args.mode & os.W_OK

    async def getxattr(self, ctx: Context) -> bytes:
        raise FsError(errno.ENODATA)

    async def setxattr(self, ctx: Context) -> None:
        raise _erofs()

    async def listxattr(self, ctx: Context) -> list[str]:
        return []

    async def removexattr(self, ctx: Context) -> None:
        raise _erofs()


class SyntheticDir(VirtualHandler):
    """The mount root and every directory that exists only because a route passes through it."""

    def __init__(self) -> None:
        self.mtime_ns = time.time_ns()

    async def getattr(self, ctx: Context) -> Attr:
        return Attr.directory(0o555, mtime_ns=self.mtime_ns)

    async def open(self, ctx: Context) -> Opened:
        raise FsError(errno.EISDIR)

    async def opendir(self, ctx: Context) -> Opened:
        return Opened()

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        return []  # The dispatcher adds the child routes' names
