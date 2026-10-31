"""The pyfuse3 adapter: thin wrappers that convert arguments and results. No logic.

Only this module and `mount.py` import pyfuse3 (the optional `fuse` extra). The class implements
`pyfuse3.Operations` by duck typing rather than subclassing, so it type-checks where pyfuse3
isn't installed. Users never subclass it: routes, middleware and handlers are the extension
points.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pyfuse3

from sluice.context import Caller, SetattrArgs
from sluice.dispatch import Dispatcher, Entry, FileInfo
from sluice.errors import FsError

log = logging.getLogger(__name__)


def _guard[**P, T](fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Reply with an errno. pyfuse3 only catches FUSEError: anything else stops the mount."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except FsError as e:
            raise pyfuse3.FUSEError(e.errno) from None
        except pyfuse3.FUSEError:
            raise
        except Exception:
            log.exception("unexpected error in %s", fn.__name__)
            raise pyfuse3.FUSEError(errno.EIO) from None

    return wrapper


def _caller(ctx: pyfuse3.RequestContext) -> Caller:
    return Caller(uid=ctx.uid, gid=ctx.gid, pid=ctx.pid)


def _name(name: bytes) -> str:
    return os.fsdecode(name)


class SluiceOperations:
    supports_dot_lookup = True
    enable_writeback_cache = False
    enable_acl = False

    def __init__(self, dispatcher: Dispatcher, cache_s: float = 0.0) -> None:
        self.d = dispatcher
        self.cache_s = cache_s

    def _attrs(self, entry: Entry) -> pyfuse3.EntryAttributes:
        a = entry.attr
        out = pyfuse3.EntryAttributes()
        out.st_ino = entry.inode
        out.generation = 0
        out.entry_timeout = self.cache_s
        out.attr_timeout = self.cache_s
        out.st_mode = a.st_mode
        out.st_nlink = a.st_nlink
        out.st_uid = a.st_uid
        out.st_gid = a.st_gid
        out.st_rdev = a.st_rdev
        out.st_size = a.st_size
        out.st_blksize = a.st_blksize
        out.st_blocks = a.st_blocks
        out.st_atime_ns = a.st_atime_ns
        out.st_mtime_ns = a.st_mtime_ns
        out.st_ctime_ns = a.st_ctime_ns
        return out

    @staticmethod
    def _info(info: FileInfo) -> pyfuse3.FileInfo:
        return pyfuse3.FileInfo(fh=info.fh, direct_io=info.direct_io, keep_cache=info.keep_cache)

    def init(self) -> None:
        log.info("mounted")

    def stacktrace(self) -> None:
        log.info("stacktrace requested; not supported")

    @_guard
    async def lookup(
        self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        return self._attrs(await self.d.lookup(parent_inode, _name(name), _caller(ctx)))

    async def forget(self, inode_list: Sequence[tuple[int, int]]) -> None:
        try:
            self.d.forget(inode_list)
        except Exception:  # forget must never raise: it answers no request
            log.exception("forget failed")

    @_guard
    async def getattr(self, inode: int, ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        return self._attrs(await self.d.getattr(inode, _caller(ctx)))

    @_guard
    async def setattr(
        self,
        inode: int,
        attr: pyfuse3.EntryAttributes,
        fields: pyfuse3.SetattrFields,
        fh: int | None,
        ctx: pyfuse3.RequestContext,
    ) -> pyfuse3.EntryAttributes:
        args = SetattrArgs(
            mode=attr.st_mode if fields.update_mode else None,
            uid=attr.st_uid if fields.update_uid else None,
            gid=attr.st_gid if fields.update_gid else None,
            size=attr.st_size if fields.update_size else None,
            atime_ns=attr.st_atime_ns if fields.update_atime else None,
            mtime_ns=attr.st_mtime_ns if fields.update_mtime else None,
        )
        return self._attrs(await self.d.setattr(inode, args, _caller(ctx)))

    @_guard
    async def readlink(self, inode: int, ctx: pyfuse3.RequestContext) -> bytes:
        return os.fsencode(await self.d.readlink(inode, _caller(ctx)))

    @_guard
    async def mknod(
        self, parent_inode: int, name: bytes, mode: int, rdev: int, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        return self._attrs(await self.d.mknod(parent_inode, name, mode, rdev))

    @_guard
    async def mkdir(
        self, parent_inode: int, name: bytes, mode: int, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        return self._attrs(await self.d.mkdir(parent_inode, _name(name), mode, _caller(ctx)))

    @_guard
    async def unlink(self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext) -> None:
        await self.d.unlink(parent_inode, _name(name), _caller(ctx))

    @_guard
    async def rmdir(self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext) -> None:
        await self.d.rmdir(parent_inode, _name(name), _caller(ctx))

    @_guard
    async def symlink(
        self, parent_inode: int, name: bytes, target: bytes, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        entry = await self.d.symlink(parent_inode, _name(name), _name(target), _caller(ctx))
        return self._attrs(entry)

    @_guard
    async def rename(
        self,
        parent_inode_old: int,
        name_old: bytes,
        parent_inode_new: int,
        name_new: bytes,
        flags: int,
        ctx: pyfuse3.RequestContext,
    ) -> None:
        await self.d.rename(
            parent_inode_old,
            _name(name_old),
            parent_inode_new,
            _name(name_new),
            flags,
            _caller(ctx),
        )

    @_guard
    async def link(
        self, inode: int, new_parent_inode: int, new_name: bytes, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        return self._attrs(await self.d.link(inode, new_parent_inode, new_name))

    @_guard
    async def open(self, inode: int, flags: int, ctx: pyfuse3.RequestContext) -> pyfuse3.FileInfo:
        return self._info(await self.d.open(inode, flags, _caller(ctx)))

    @_guard
    async def create(
        self,
        parent_inode: int,
        name: bytes,
        mode: int,
        flags: int,
        ctx: pyfuse3.RequestContext,
    ) -> tuple[pyfuse3.FileInfo, pyfuse3.EntryAttributes]:
        info, entry = await self.d.create(parent_inode, _name(name), mode, flags, _caller(ctx))
        return self._info(info), self._attrs(entry)

    @_guard
    async def read(self, fh: int, off: int, size: int) -> bytes:
        return await self.d.read(fh, off, size)

    @_guard
    async def write(self, fh: int, off: int, buf: bytes) -> int:
        return await self.d.write(fh, off, buf)

    @_guard
    async def flush(self, fh: int) -> None:
        await self.d.flush(fh)

    @_guard
    async def release(self, fh: int) -> None:
        await self.d.release(fh)

    @_guard
    async def fsync(self, fh: int, datasync: bool) -> None:
        await self.d.fsync(fh, datasync)

    async def poll(self, fh: int, events: int, poll_handle: Any) -> int:
        raise pyfuse3.FUSEError(errno.ENOSYS)

    @_guard
    async def opendir(self, inode: int, ctx: pyfuse3.RequestContext) -> int:
        return await self.d.opendir(inode, _caller(ctx))

    @_guard
    async def readdir(self, fh: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        for item in await self.d.readdir(fh, start_id):
            attrs = self._attrs(Entry(item.inode, item.attr))
            if not pyfuse3.readdir_reply(token, os.fsencode(item.name), attrs, item.next_id):
                break
            self.d.readdir_ref(item.inode)

    @_guard
    async def releasedir(self, fh: int) -> None:
        await self.d.releasedir(fh)

    async def fsyncdir(self, fh: int, datasync: bool) -> None:
        return None

    @_guard
    async def statfs(self, ctx: pyfuse3.RequestContext) -> pyfuse3.StatvfsData:
        s = await self.d.statfs(_caller(ctx))
        out = pyfuse3.StatvfsData()
        out.f_bsize = s.f_bsize
        out.f_frsize = s.f_frsize
        out.f_blocks = s.f_blocks
        out.f_bfree = s.f_bfree
        out.f_bavail = s.f_bavail
        out.f_files = s.f_files
        out.f_ffree = s.f_ffree
        out.f_favail = s.f_favail
        out.f_namemax = s.f_namemax
        return out

    @_guard
    async def setxattr(
        self, inode: int, name: bytes, value: bytes, ctx: pyfuse3.RequestContext
    ) -> None:
        await self.d.setxattr(inode, _name(name), value, _caller(ctx))

    @_guard
    async def getxattr(self, inode: int, name: bytes, ctx: pyfuse3.RequestContext) -> bytes:
        return await self.d.getxattr(inode, _name(name), _caller(ctx))

    @_guard
    async def listxattr(self, inode: int, ctx: pyfuse3.RequestContext) -> list[bytes]:
        return [os.fsencode(n) for n in await self.d.listxattr(inode, _caller(ctx))]

    @_guard
    async def removexattr(self, inode: int, name: bytes, ctx: pyfuse3.RequestContext) -> None:
        await self.d.removexattr(inode, _name(name), _caller(ctx))

    @_guard
    async def access(self, inode: int, mode: int, ctx: pyfuse3.RequestContext) -> bool:
        return await self.d.access(inode, mode, _caller(ctx))
