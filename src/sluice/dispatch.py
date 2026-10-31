"""The dispatcher: one async method per FUSE operation, free of pyfuse3.

Each call turns its inode or handle into a virtual path, routes it, builds a `Context`, runs the
middleware pipeline around the operation, and keeps the inode and handle tables in step. Failures
are raised as `FsError`; `fs.py` turns them into errno replies.
"""

from __future__ import annotations

import errno
import itertools
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sluice.backend import Passthrough
from sluice.context import (
    AccessArgs,
    Attr,
    Caller,
    Context,
    CreateArgs,
    DirEntry,
    FsyncArgs,
    MkdirArgs,
    OpenArgs,
    Opened,
    ReadArgs,
    RenameArgs,
    SetattrArgs,
    StatFs,
    SymlinkArgs,
    WriteArgs,
    XattrArgs,
)
from sluice.errors import FsError
from sluice.inodes import Handle, HandleTable, InodeTable
from sluice.middleware import run_pipeline
from sluice.routing import Resolution, RouteTable
from sluice.virtual import SyntheticDir, VirtualHandler

log = logging.getLogger(__name__)

RENAME_EXCHANGE = 2  # linux/fs.h

Impl = VirtualHandler | Passthrough


@dataclass(frozen=True)
class Entry:
    inode: int
    attr: Attr


@dataclass(frozen=True)
class FileInfo:
    fh: int
    direct_io: bool
    keep_cache: bool


@dataclass(frozen=True)
class Listed:
    """One `readdir` entry: `next_id` is what the kernel passes back to continue after it."""

    name: str
    inode: int
    attr: Attr
    next_id: int


async def _orphan_getattr(impl: Impl, ctx: Context, handle: Handle) -> Attr:
    if isinstance(impl, Passthrough):
        return await impl.fgetattr(ctx, handle.opened)
    return await impl.getattr(ctx)


async def _orphan_setattr(impl: Impl, ctx: Context, handle: Handle) -> Attr:
    if isinstance(impl, Passthrough):
        return await impl.fsetattr(ctx, handle.opened)
    return await impl.setattr(ctx)


class Dispatcher:
    def __init__(self, table: RouteTable, passthrough: Passthrough) -> None:
        self.table = table
        self.io = passthrough
        self.inodes = InodeTable()
        self.handles = HandleTable()
        self._ids = itertools.count(1)
        self._synthetic = SyntheticDir()

    # Plumbing

    def _resolve(self, path: PurePosixPath) -> Resolution:
        res = self.table.resolve(path)
        if res is None:
            raise FsError(errno.ENOENT)
        return res

    def _impl(self, res: Resolution) -> Impl:
        if res.kind == "backed":
            return self.io
        if res.kind == "virtual" and res.handler is not None:
            return res.handler
        return self._synthetic

    def _context(
        self, op: str, res: Resolution, inode: int, args: Any, caller: Caller | None
    ) -> Context:
        return Context(
            op,
            res,
            config=self.table.config,
            inode=inode,
            args=args,
            caller=caller,
            correlation_id=f"{next(self._ids):x}",
        )

    async def _call(
        self,
        op: str,
        path: PurePosixPath,
        caller: Caller | None,
        operation: Callable[[Impl, Context], Awaitable[Any]],
        args: Any = None,
    ) -> Any:
        res = self._resolve(path)
        impl = self._impl(res)
        ctx = self._context(op, res, self.inodes.find(path), args, caller)
        return await run_pipeline(ctx, lambda c: operation(impl, c))

    async def _on_handle(
        self,
        op: str,
        fh: int,
        operation: Callable[[Impl, Context, Handle], Awaitable[Any]],
        args: Any = None,
        caller: Caller | None = None,
    ) -> Any:
        # An open file keeps the resolution it was opened with, as POSIX keeps it across a
        # rename; the chain and param() callbacks still run on every call. `caller` is the
        # requester when the kernel names one; otherwise the opener.
        handle = self.handles.get(fh)
        ctx = self._context(op, handle.resolution, handle.inode, args, caller or handle.caller)
        return await run_pipeline(ctx, lambda c: operation(handle.impl, c, handle))

    def _entry(self, path: PurePosixPath, attr: Attr) -> Entry:
        return Entry(self.inodes.lookup(path), attr)

    # Path operations

    async def lookup(self, parent: int, name: str, caller: Caller | None = None) -> Entry:
        path = self.inodes.child(parent, name)
        attr = await self._call("lookup", path, caller, lambda i, c: i.getattr(c))
        return self._entry(path, attr)

    def forget(self, inodes: Sequence[tuple[int, int]]) -> None:
        for inode, count in inodes:
            self.inodes.forget(inode, count)

    async def getattr(self, inode: int, caller: Caller | None = None) -> Entry:
        fh = self._orphan(inode)
        if fh is not None:
            return Entry(inode, await self._on_handle("getattr", fh, _orphan_getattr, None, caller))
        path = self.inodes.path(inode)
        return Entry(inode, await self._call("getattr", path, caller, lambda i, c: i.getattr(c)))

    async def setattr(
        self,
        inode: int,
        args: SetattrArgs,
        caller: Caller | None = None,
        fh: int | None = None,
    ) -> Entry:
        orphan = self._orphan(inode, fh)
        if orphan is not None:
            attr = await self._on_handle("setattr", orphan, _orphan_setattr, args, caller)
            return Entry(inode, attr)
        path = self.inodes.path(inode)
        attr = await self._call("setattr", path, caller, lambda i, c: i.setattr(c), args)
        return Entry(inode, attr)

    def _orphan(self, inode: int, fh: int | None = None) -> int | None:
        """The handle to serve a detached inode through: its path now names nothing, or
        another file, but an open file still works on what it opened (`fstat`, `ftruncate`)."""
        if not self.inodes.detached(inode):
            return None
        return fh if fh is not None else self.handles.find(inode)

    async def readlink(self, inode: int, caller: Caller | None = None) -> str:
        path = self.inodes.path(inode)
        result: str = await self._call("readlink", path, caller, lambda i, c: i.readlink(c))
        return result

    async def mkdir(self, parent: int, name: str, mode: int, caller: Caller | None = None) -> Entry:
        path = self.inodes.child(parent, name)
        attr = await self._call("mkdir", path, caller, lambda i, c: i.mkdir(c), MkdirArgs(mode))
        return self._entry(path, attr)

    async def rmdir(self, parent: int, name: str, caller: Caller | None = None) -> None:
        path = self.inodes.child(parent, name)
        await self._call("rmdir", path, caller, lambda i, c: i.rmdir(c))
        self.inodes.unlink(path)

    async def unlink(self, parent: int, name: str, caller: Caller | None = None) -> None:
        path = self.inodes.child(parent, name)
        await self._call("unlink", path, caller, lambda i, c: i.unlink(c))
        self.inodes.unlink(path)

    async def symlink(
        self, parent: int, name: str, target: str, caller: Caller | None = None
    ) -> Entry:
        path = self.inodes.child(parent, name)
        args = SymlinkArgs(target)
        attr = await self._call("symlink", path, caller, lambda i, c: i.symlink(c), args)
        return self._entry(path, attr)

    async def rename(
        self,
        parent_old: int,
        name_old: str,
        parent_new: int,
        name_new: str,
        flags: int = 0,
        caller: Caller | None = None,
    ) -> None:
        old = self.inodes.child(parent_old, name_old)
        new = self.inodes.child(parent_new, name_new)
        src, dst = self._resolve(old), self._resolve(new)
        if src.kind != "backed" or dst.kind != "backed" or src.backend_root != dst.backend_root:
            # Tools fall back to copy and delete, which go through the pipeline per path.
            raise FsError(errno.EXDEV, "rename across backends")
        args = RenameArgs(target=new, flags=flags, target_real_path=dst.real_path)
        ctx = self._context("rename", src, self.inodes.find(old), args, caller)
        target_ctx = self._context("rename", dst, self.inodes.find(new), args, caller)
        await run_pipeline(ctx, self.io.rename, also=[target_ctx])
        self.inodes.rename(old, new, exchange=bool(flags & RENAME_EXCHANGE))

    async def link(self, *args: object) -> Entry:
        raise FsError(errno.ENOTSUP, "hard links are not supported")

    async def mknod(self, *args: object) -> Entry:
        raise FsError(errno.ENOTSUP, "device and special files are not supported")

    # Files

    async def _open(
        self,
        op: str,
        path: PurePosixPath,
        caller: Caller | None,
        operation: Callable[[Impl, Context], Awaitable[Any]],
        args: Any,
    ) -> tuple[Any, Handle]:
        res = self._resolve(path)
        impl = self._impl(res)
        ctx = self._context(op, res, self.inodes.find(path), args, caller)
        opened: list[Opened] = []

        async def run(c: Context) -> Any:
            result = await operation(impl, c)
            opened.append(result[0] if isinstance(result, tuple) else result)
            return result

        try:
            result = await run_pipeline(ctx, run)
            final = result[0] if isinstance(result, tuple) else result
            if not isinstance(final, Opened):
                log.error("%s: a post hook returned %s, not Opened", op, type(final).__name__)
                raise FsError(errno.EIO, "post hook returned no Opened")
        except FsError:
            # The operation opened something but a post hook failed: the kernel never gets a
            # handle to release, so close it here.
            for o in opened:
                await self._release(impl, o, dir_=op == "opendir")
            raise
        # A post hook may replace the result (virtual content, say): the kernel releases only
        # the replacement, so close an fd it dropped. An fd-less original can't be told apart
        # from a copy of it, so it isn't released: releasing it twice is worse.
        for o in opened:
            if o.fd is not None and o.fd != final.fd:
                await self._release(impl, o, dir_=op == "opendir")
        handle = Handle(res, 0, impl, final, ctx.caller)
        return result, handle

    async def _release(self, impl: Impl, opened: Opened, dir_: bool) -> None:
        try:
            await (impl.releasedir(opened) if dir_ else impl.release(opened))
        except Exception:
            log.exception("release failed; ignored")

    def _file_info(self, handle: Handle) -> FileInfo:
        fh = self.handles.add(handle)
        return FileInfo(fh, handle.opened.direct_io, handle.opened.keep_cache)

    async def open(self, inode: int, flags: int, caller: Caller | None = None) -> FileInfo:
        path = self.inodes.path(inode)
        _, handle = await self._open("open", path, caller, lambda i, c: i.open(c), OpenArgs(flags))
        handle.inode = inode
        return self._file_info(handle)

    async def create(
        self, parent: int, name: str, mode: int, flags: int, caller: Caller | None = None
    ) -> tuple[FileInfo, Entry]:
        path = self.inodes.child(parent, name)
        args = CreateArgs(mode, flags)
        (_, attr), handle = await self._open("create", path, caller, lambda i, c: i.create(c), args)
        entry = self._entry(path, attr)
        handle.inode = entry.inode
        return self._file_info(handle), entry

    async def read(self, fh: int, offset: int, size: int) -> bytes:
        result: bytes = await self._on_handle(
            "read", fh, lambda i, c, h: i.read(c, h.opened), ReadArgs(offset, size)
        )
        return result

    async def write(self, fh: int, offset: int, data: bytes) -> int:
        result: int = await self._on_handle(
            "write", fh, lambda i, c, h: i.write(c, h.opened), WriteArgs(offset, data)
        )
        return result

    async def flush(self, fh: int) -> None:
        await self._on_handle("flush", fh, lambda i, c, h: i.flush(c, h.opened))

    async def fsync(self, fh: int, datasync: bool) -> None:
        args = FsyncArgs(datasync)
        await self._on_handle("fsync", fh, lambda i, c, h: i.fsync(c, h.opened), args)

    async def release(self, fh: int) -> None:
        # Cleanup bypasses middleware: the kernel can't act on its failure.
        handle = self.handles.pop(fh)
        if handle is not None:
            await self._release(handle.impl, handle.opened, dir_=False)

    # Directories

    async def opendir(self, inode: int, caller: Caller | None = None) -> int:
        path = self.inodes.path(inode)
        _, handle = await self._open("opendir", path, caller, lambda i, c: i.opendir(c), None)
        handle.inode = inode
        return self.handles.add(handle)

    async def readdir(self, fh: int, start: int = 0) -> list[Listed]:
        handle = self.handles.get(fh)
        if start == 0 or handle.entries is None:
            handle.entries = await self._on_handle("readdir", fh, self._list)
        base = handle.resolution.path
        return [
            Listed(e.name, self.inodes.ensure(base / e.name), e.attr, i + 1)
            for i, e in enumerate(handle.entries)
            if i >= start
        ]

    def readdir_ref(self, inode: int) -> None:
        """The kernel took a `readdir` entry: that counts as a lookup."""
        self.inodes.ref(inode)

    async def _list(self, impl: Impl, ctx: Context, handle: Handle) -> list[DirEntry]:
        entries = await impl.readdir(ctx, handle.opened)
        children = ctx.resolution.children
        if children:
            # A child route's name hides a real entry of the same name.
            entries = [e for e in entries if e.name not in children]
            for name in children:
                entries.append(DirEntry(name, await self._placeholder(ctx.path / name)))
        return entries

    async def _placeholder(self, path: PurePosixPath) -> Attr:
        """Attributes for a child route in a listing, without running its chain.

        The kernel looks the entry up (through the pipeline) before using it.
        """
        res = self.table.resolve(path)
        if res is not None and res.kind == "virtual":
            return Attr.file(0o444, mtime_ns=self._synthetic.mtime_ns)
        if res is not None and res.real_path is not None:
            try:
                return await self.io.lstat(res.real_path)
            except OSError:
                pass
        return Attr.directory(0o555, mtime_ns=self._synthetic.mtime_ns)

    async def releasedir(self, fh: int) -> None:
        handle = self.handles.pop(fh)
        if handle is not None:
            await self._release(handle.impl, handle.opened, dir_=True)

    # Everything else

    async def statfs(self, caller: Caller | None = None) -> StatFs:
        async def synthetic(impl: Impl, ctx: Context) -> StatFs:
            return StatFs()

        result: StatFs = await self._call("statfs", PurePosixPath("/"), caller, synthetic)
        return result

    async def access(self, inode: int, mode: int, caller: Caller | None = None) -> bool:
        path = self.inodes.path(inode)
        args = AccessArgs(mode)
        result: bool = await self._call("access", path, caller, lambda i, c: i.access(c), args)
        return result

    async def getxattr(self, inode: int, name: str, caller: Caller | None = None) -> bytes:
        path = self.inodes.path(inode)
        args = XattrArgs(name)
        result: bytes = await self._call("getxattr", path, caller, lambda i, c: i.getxattr(c), args)
        return result

    async def setxattr(
        self, inode: int, name: str, value: bytes, caller: Caller | None = None
    ) -> None:
        path = self.inodes.path(inode)
        args = XattrArgs(name, value)
        await self._call("setxattr", path, caller, lambda i, c: i.setxattr(c), args)

    async def listxattr(self, inode: int, caller: Caller | None = None) -> list[str]:
        path = self.inodes.path(inode)
        result: list[str] = await self._call("listxattr", path, caller, lambda i, c: i.listxattr(c))
        return result

    async def removexattr(self, inode: int, name: str, caller: Caller | None = None) -> None:
        path = self.inodes.path(inode)
        args = XattrArgs(name)
        await self._call("removexattr", path, caller, lambda i, c: i.removexattr(c), args)
