"""strmfs as middleware: show each video `X.mkv` as `X.mkv.strm`, a file holding its location.

An example, not part of the sluice package: copy it beside your app module and adapt it.
Kodi, Jellyfin and similar players read a `.strm` file as "play this location". Directories
pass through; every other file is hidden from listings and lookups alike. The mount is
read-only.

    from strm import Strm

    app.route("/media", backend="/srv/media", middlewares=[Strm("http://media.example.org/{rel}")])

`{rel}` is the video's path below the backend, URL-quoted (`Films/Heat%202.mkv`); `{name}` is
its file name. All I/O goes through `ctx.backend`, so nothing is followed out of the backend: a
symlinked video counts while its target stays inside it. Real entries named `*.strm` are hidden:
the name is the generated file's.

Put `Strm` after any authorisation middleware on the route: it answers `.strm` lookups and
opens itself (`ctx.respond()`), which skips the `pre` hooks after it.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from sluice import Attr, BackendView, Context, DirEntry, FsError, Middleware, Opened

SUFFIX = ".strm"
VIDEO = frozenset({".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg"})
VIDEO |= {".ts", ".webm", ".wmv"}
# Every op that changes the tree, and every path op. Handle ops (read, flush, fsync) need no
# check: only an open this middleware allowed made the handle.
_WRITES = frozenset(
    {"setattr", "mkdir", "rmdir", "unlink", "symlink", "rename", "create", "write"}
    | {"setxattr", "removexattr"}
)
_READS = frozenset({"lookup", "getattr", "readlink", "open", "opendir", "access"})
_XATTR_READS = frozenset({"getxattr", "listxattr"})


class Strm(Middleware):
    """Serve `X.<video>.strm` files whose content is `template` filled in for `X.<video>`."""

    ops = _WRITES | _READS | _XATTR_READS | {"readdir"}

    def __init__(self, template: str, *, video: Iterable[str] = VIDEO) -> None:
        self.template = template
        self.video = frozenset(s.lower() for s in video)
        try:  # A bad placeholder fails here, at route setup, not as EIO on every listing.
            self.body(PurePosixPath("check.mkv"))
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"Strm template {template!r}: {e!r}") from None

    def body(self, rel: PurePosixPath) -> bytes:
        """The `.strm` content for the video at `rel` below the backend."""
        location = self.template.format(rel=quote(rel.as_posix()), name=rel.name)
        return f"{location}\n".encode()

    def is_video(self, name: str) -> bool:
        return PurePosixPath(name).suffix.lower() in self.video

    async def pre(self, ctx: Context) -> None:
        if ctx.op in _WRITES or (ctx.op == "open" and ctx.args.flags & (os.O_ACCMODE | os.O_TRUNC)):
            raise FsError(errno.EROFS, "strm mounts are read-only")
        if ctx.op == "access" and ctx.args.mode & os.W_OK:
            ctx.respond(False)  # Every write would be EROFS
            return
        backend, real = ctx.backend, ctx.real_path
        if backend is None or real is None or ctx.op == "readdir":
            return
        if real.name.endswith(SUFFIX) and self.is_video(real.name.removesuffix(SUFFIX)):
            await self._strm(ctx, backend, real.with_name(real.name.removesuffix(SUFFIX)))
            return
        # Not a .strm name: only directories are shown, so a listing and a lookup agree.
        if not stat.S_ISDIR((await backend.lstat(real)).st_mode):
            raise FsError(errno.ENOENT)

    async def _strm(self, ctx: Context, backend: BackendView, video: Path) -> None:
        try:
            attr = await backend.stat(video)
        except FsError:
            attr = None  # Missing, dangling, a loop, or a link out: the listing hides them too
        if attr is None or not stat.S_ISREG(attr.st_mode):
            raise FsError(errno.ENOENT)
        body = self.body(PurePosixPath(video.relative_to(backend.root)))
        if ctx.op in {"lookup", "getattr"}:
            ctx.respond(Attr.file(0o444, size=len(body), mtime_ns=attr.st_mtime_ns))
        elif ctx.op == "open":
            ctx.respond(Opened(data=body, direct_io=True))
        elif ctx.op == "access":
            ctx.respond(not ctx.args.mode & os.X_OK)  # 0o444; W_OK was refused above
        elif ctx.op == "listxattr":
            ctx.respond([])
        elif ctx.op == "getxattr":
            raise FsError(errno.ENODATA)
        elif ctx.op == "opendir":
            raise FsError(errno.ENOTDIR)
        else:  # readlink
            raise FsError(errno.EINVAL)

    async def post(self, ctx: Context, result: Any) -> Any:
        if ctx.op != "readdir" or ctx.backend is None or ctx.real_path is None:
            return result
        backend = ctx.backend
        here = PurePosixPath(ctx.real_path.relative_to(backend.root))
        entries: list[DirEntry] = []
        for entry in result:
            if entry.name.endswith(SUFFIX):
                continue  # A real *.strm would clash with a generated one
            mode = entry.attr.st_mode
            if stat.S_ISDIR(mode):
                entries.append(entry)
                continue
            if not self.is_video(entry.name):
                continue
            attr = entry.attr
            if stat.S_ISLNK(mode):
                try:
                    attr = await backend.stat(here / entry.name)
                except FsError:
                    continue  # Dangling, or leaves the backend
            if not stat.S_ISREG(attr.st_mode):
                continue
            body = self.body(here / entry.name)
            strm = Attr.file(0o444, size=len(body), mtime_ns=attr.st_mtime_ns)
            entries.append(DirEntry(entry.name + SUFFIX, strm))
        return entries

    def __repr__(self) -> str:
        return f"Strm({self.template!r})"
