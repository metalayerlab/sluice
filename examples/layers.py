"""ShadowFS-style virtual layers: views of a backed route by type, date or tag, as symlinks.

An example, not part of the sluice package: copy it beside your app module and adapt it.
A `Layer` is a `VirtualHandler` for a `{rest*}` route. It lists a tree that `classify` builds
from an index of the source directory, and every file in it is a **relative** symlink back into
the backed route (`by-type/py/project.py -> ../../src/project.py`). Reads therefore go through
the backed route and its middleware, and the app needn't know the mountpoint.

    from layers import Layer, by_date, by_type

    app.route("/src", backend="/srv/project")
    app.route("/by-type/{rest*}", virtual=True, handler=Layer("/srv/project", "/src", by_type))
    app.route("/by-date/{rest*}", virtual=True, handler=Layer("/srv/project", "/src", by_date))

The index is built off the event loop on first use, and rebuilt in the background once it is
older than `refresh` seconds; calls meanwhile see the previous one. The scan never follows a
symlink and indexes regular files only. A scan can't be cancelled: an unmount during one waits
for it to finish. Names two files share in one view are marked with their source directory
(`project@docs.py`), so a name never comes to mean a different file.

A view lists names its source route's middleware might hide (the links lead through that
middleware, so reads are still checked): give the view route the same middleware if names are
sensitive.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import posixpath
import stat
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from sluice import Attr, Context, DirEntry, FsError, Opened, VirtualHandler

log = logging.getLogger(__name__)

# Where a file appears: zero or more view directories ("py", "2026/10/31"), from its path below
# the source, its real path and its lstat. Runs in a worker thread, so it may do I/O.
type Classify = Callable[[PurePosixPath, Path, os.stat_result], Iterable[str]]
type Tree = dict[tuple[str, ...], dict[str, PurePosixPath | None]]  # dir -> name -> file or dir


def by_type(rel: PurePosixPath, real: Path, st: os.stat_result) -> Iterable[str]:
    """`py/`, `md/`, ... by lower-case extension; `none/` without one."""
    return [rel.suffix[1:].lower() or "none"]


def by_date(rel: PurePosixPath, real: Path, st: os.stat_result) -> Iterable[str]:
    """`YYYY/MM/DD/` by modification time, in local time."""
    return [time.strftime("%Y/%m/%d", time.localtime(st.st_mtime))]


def by_tag(rel: PurePosixPath, real: Path, st: os.stat_result) -> Iterable[str]:
    """One directory per tag in the `user.tags` xattr (`work,urgent`); none without it."""
    try:
        raw = os.getxattr(real, "user.tags", follow_symlinks=False)
    except OSError:
        return []
    tags = (s.strip() for s in raw.decode(errors="replace").split(","))
    return list(dict.fromkeys(t for t in tags if t))


def _qualified(name: str, rel: PurePosixPath) -> str:
    """`name` marked with its source directory: `project@docs.py`, `project@.py` at the top."""
    p = PurePosixPath(name)
    return f"{p.stem}@{'~'.join(rel.parent.parts)}{p.suffix}"


def scan(source: Path, classify: Classify) -> Tree:
    """The view tree for every regular file below `source`. Never follows a symlink.

    A name held by one file is the file's own; names two files share, or a view directory
    has, are all marked with their source directory instead. A name therefore never comes to
    mean a different file when files are added: it means its file, or nothing.
    """
    placed: dict[tuple[str, ...], dict[str, list[PurePosixPath]]] = {(): {}}

    def walk(directory: Path, rel: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as it:
                found = sorted(it, key=lambda e: e.name)
        except OSError as e:
            log.warning("layers: can't scan %s: %s", directory, e.strerror)
            return
        for entry in found:
            here = rel / entry.name
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path), here)
            elif entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                views = {_parts(view): None for view in classify(here, Path(entry.path), st)}
                for where in views:  # Each view once, however often classify names it
                    placed.setdefault(where, {}).setdefault(entry.name, []).append(here)

    walk(source, PurePosixPath())
    tree: Tree = {}
    for where in list(placed):
        for depth in range(len(where) + 1):  # Each directory on the way
            tree.setdefault(where[:depth], {})
            if depth:
                tree[where[: depth - 1]][where[depth - 1]] = None
    for where, names in placed.items():
        entries = tree[where]
        for name, rels in names.items():
            if len(rels) == 1 and name not in entries:
                entries[name] = rels[0]
            else:
                entries.update((_qualified(name, rel), rel) for rel in rels)
    return tree


def _parts(view: str) -> tuple[str, ...]:
    return tuple(p for p in PurePosixPath(view).parts if p not in {"/", ".", ".."})


class Layer(VirtualHandler):
    """A read-only view of `source` (served by the backed route at `target`) as symlinks."""

    def __init__(
        self,
        source: str | Path,
        target: str,
        classify: Classify,
        *,
        refresh: float = 300,
        param: str = "rest",
    ) -> None:
        self.source = Path(source)
        self.target = PurePosixPath(target)
        self.classify = classify
        self.refresh = refresh
        self.param = param
        self._tree: Tree | None = None
        self._built = 0.0
        self._built_ns = 0
        self._lock = asyncio.Lock()
        self._rebuild: asyncio.Task[None] | None = None

    async def index(self) -> Tree:
        """The current tree: built on first use, then refreshed in the background."""
        tree = self._tree
        if tree is None:
            async with self._lock:  # One scan, however many calls arrive first
                tree = self._tree or await self._build()
        elif time.monotonic() - self._built > self.refresh and self._rebuild is None:
            self._rebuild = asyncio.create_task(self._refresh())
        return tree

    async def _build(self) -> Tree:
        tree = await asyncio.to_thread(scan, self.source, self.classify)
        self._tree, self._built, self._built_ns = tree, time.monotonic(), time.time_ns()
        return tree

    async def _refresh(self) -> None:
        try:
            await self._build()
        except Exception:
            log.exception("layers: rebuilding the index of %s failed", self.source)
            self._built = time.monotonic()  # Retry after another `refresh`, not on every call
        finally:
            self._rebuild = None

    async def _node(self, ctx: Context) -> tuple[tuple[str, ...], PurePosixPath | None]:
        """The view path and its entry: `None` for a directory, the source file for a link."""
        where = tuple(PurePosixPath(ctx.params.get(self.param, "") or ".").parts)
        where = tuple(p for p in where if p != ".")
        tree = await self.index()
        if where in tree:
            return where, None
        file = tree.get(where[:-1], {}).get(where[-1]) if where else None
        if file is None:
            raise FsError(errno.ENOENT)
        return where, file

    def _link(self, at: PurePosixPath, file: PurePosixPath) -> str:
        """The relative link at virtual path `at` to `file` in the backed route."""
        return posixpath.relpath(self.target / file, at.parent)

    def _attr(self, link: str | None) -> Attr:
        """A directory's attributes, or a symlink's whose target is `link`."""
        mtime = self._built_ns
        if link is None:
            return Attr.directory(0o555, mtime_ns=mtime)
        return Attr(
            stat.S_IFLNK | 0o777,
            st_uid=os.getuid(),
            st_gid=os.getgid(),
            st_size=len(os.fsencode(link)),
            st_atime_ns=mtime,
            st_mtime_ns=mtime,
            st_ctime_ns=mtime,
        )

    async def getattr(self, ctx: Context) -> Attr:
        _, file = await self._node(ctx)
        return self._attr(None if file is None else self._link(ctx.path, file))

    async def readlink(self, ctx: Context) -> str:
        _, file = await self._node(ctx)
        if file is None:
            raise FsError(errno.EINVAL, "a directory")
        return self._link(ctx.path, file)

    async def open(self, ctx: Context) -> Opened:
        # The kernel follows a link before it opens, so only a directory gets here.
        await self._node(ctx)
        raise FsError(errno.EISDIR)

    async def opendir(self, ctx: Context) -> Opened:
        _, file = await self._node(ctx)
        if file is not None:
            raise FsError(errno.ENOTDIR)
        return Opened()

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        where, _ = await self._node(ctx)
        tree = await self.index()
        return [
            DirEntry(name, self._attr(None if file is None else self._link(ctx.path / name, file)))
            for name, file in sorted(tree.get(where, {}).items())
        ]

    def __repr__(self) -> str:
        return f"Layer({str(self.source)!r}, {str(self.target)!r})"
