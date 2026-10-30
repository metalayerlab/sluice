"""Inodes and file handles.

FUSE calls name files by inode or handle, never by path; the router needs paths. `InodeTable`
keeps the two consistent across create, rename and unlink. Inodes are per virtual path: two
virtual paths to one real file are two inodes.
"""

from __future__ import annotations

import errno
import itertools
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from sluice.context import Caller, DirEntry, Opened
from sluice.errors import FsError

if TYPE_CHECKING:
    from sluice.backend import Passthrough
    from sluice.routing import Resolution
    from sluice.virtual import VirtualHandler

ROOT_INODE = 1
ROOT = PurePosixPath("/")


class InodeTable:
    def __init__(self) -> None:
        self._path: dict[int, PurePosixPath] = {ROOT_INODE: ROOT}
        self._inode: dict[PurePosixPath, int] = {ROOT: ROOT_INODE}
        self._lookups: dict[int, int] = {}
        self._next = itertools.count(ROOT_INODE + 1)

    def path(self, inode: int) -> PurePosixPath:
        try:
            return self._path[inode]
        except KeyError:
            raise FsError(errno.ENOENT, f"inode {inode} is gone") from None

    def child(self, parent: int, name: str) -> PurePosixPath:
        base = self.path(parent)
        if name == ".":
            return base
        if name == "..":
            return base.parent
        return base / name

    def find(self, path: PurePosixPath) -> int:
        """The inode of `path`, or 0 if it has none yet."""
        return self._inode.get(path, 0)

    def ensure(self, path: PurePosixPath) -> int:
        """The inode of `path`, assigning one if needed, without counting a lookup."""
        inode = self._inode.get(path)
        if inode is None:
            inode = next(self._next)
            self._inode[path] = inode
            self._path[inode] = path
        return inode

    def lookup(self, path: PurePosixPath) -> int:
        """`ensure()` and count one kernel lookup."""
        inode = self.ensure(path)
        self.ref(inode)
        return inode

    def ref(self, inode: int) -> None:
        self._lookups[inode] = self._lookups.get(inode, 0) + 1

    def forget(self, inode: int, count: int) -> None:
        if inode == ROOT_INODE:
            return
        left = self._lookups.get(inode, 0) - count
        if left > 0:
            self._lookups[inode] = left
            return
        self._lookups.pop(inode, None)
        path = self._path.pop(inode, None)
        if path is not None and self._inode.get(path) == inode:
            del self._inode[path]

    def _detach(self, path: PurePosixPath) -> None:
        """Drop `path` and everything under it. Their inodes live on until forgotten."""
        for p in [p for p in self._inode if p == path or p.is_relative_to(path)]:
            inode = self._inode.pop(p)
            del self._path[inode]

    def unlink(self, path: PurePosixPath) -> None:
        self._detach(path)

    def rename(self, old: PurePosixPath, new: PurePosixPath, exchange: bool = False) -> None:
        moving = {p: i for p, i in self._inode.items() if p == old or p.is_relative_to(old)}
        swapped = {}
        if exchange:
            swapped = {p: i for p, i in self._inode.items() if p == new or p.is_relative_to(new)}
        for p in [*moving, *swapped]:
            del self._inode[p]
        if not exchange:
            self._detach(new)
        for p, i in moving.items():
            moved = new / p.relative_to(old)
            self._inode[moved] = i
            self._path[i] = moved
        for p, i in swapped.items():
            moved = old / p.relative_to(new)
            self._inode[moved] = i
            self._path[i] = moved

    def __len__(self) -> int:
        return len(self._path)


@dataclass
class Handle:
    """An open file or directory: what it resolved to when opened, and the backend's handle."""

    resolution: Resolution
    inode: int
    impl: VirtualHandler | Passthrough
    opened: Opened
    caller: Caller
    entries: list[DirEntry] | None = field(default=None)


class HandleTable:
    def __init__(self) -> None:
        self._handles: dict[int, Handle] = {}
        self._next = itertools.count(1)

    def add(self, handle: Handle) -> int:
        fh = next(self._next)
        self._handles[fh] = handle
        return fh

    def get(self, fh: int) -> Handle:
        try:
            return self._handles[fh]
        except KeyError:
            raise FsError(errno.EBADF) from None

    def pop(self, fh: int) -> Handle | None:
        return self._handles.pop(fh, None)

    def __len__(self) -> int:
        return len(self._handles)
