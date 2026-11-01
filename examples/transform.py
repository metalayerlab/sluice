"""ShadowFS-style transforms: rewrite a file's content on read (Markdown to HTML, decompress).

An example, not part of the sluice package: copy it beside your app module and adapt it.
A matching file is read whole through `ctx.backend`, passed through `fn` in a worker thread,
and served as a read-only snapshot; `getattr` and listings report the transformed size, so
`ls -l` and readers agree. Several `Transform`s on one route form a pipeline: the last in the
chain runs first, and each hands its output to the one before it.

    from transform import Transform

    app.route("/docs", backend="/srv/docs", middlewares=[
        Transform(markdown_to_html, match="*.md"),
    ])

Outputs are cached by `(real_path, st_mtime_ns, st_size)`, bounded by `cache_bytes`. A file
larger than `max_bytes` is refused with `EFBIG` on open, so a reader never mistakes raw bytes
for transformed ones; its size is reported raw. A matching file can't be opened for writing,
truncated or otherwise changed (`EROFS`). Note that a cold `ls -l` runs `fn` on every matching
file in the directory.
"""

from __future__ import annotations

import asyncio
import errno
import fnmatch
import os
import stat
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from sluice import Attr, BackendView, Context, DirEntry, FsError, Middleware, Opened

MiB = 1 << 20
# ctx.state: real path -> (its raw attributes, the inner transform's output or None if refused)
_STATE = "transform.runs"


class Transform(Middleware):
    """Serve files whose name matches `match` as `fn(content)`."""

    ops = frozenset({"lookup", "getattr", "setattr", "open", "readdir"})

    def __init__(
        self,
        fn: Callable[[bytes], bytes],
        *,
        match: str = "*",
        max_bytes: int = 64 * MiB,
        cache_bytes: int = 64 * MiB,
    ) -> None:
        self.fn = fn
        self.match = match
        self.max_bytes = max_bytes
        self.cache_bytes = cache_bytes
        self._cache: OrderedDict[tuple[Path, int, int], bytes] = OrderedDict()
        self._cached = 0

    def matches(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name, self.match)

    async def pre(self, ctx: Context) -> None:
        real = ctx.real_path
        if real is None or not self.matches(real.name):
            return
        # truncate, chmod, touch: the file is a view of its source, and read-only.
        writing = ctx.op == "open" and ctx.args.flags & (os.O_ACCMODE | os.O_TRUNC)
        if ctx.op == "setattr" or writing:
            raise FsError(errno.EROFS, "a transformed file is read-only")

    async def post(self, ctx: Context, result: Any) -> Any:
        backend, real = ctx.backend, ctx.real_path
        if backend is None or real is None:
            return result
        if ctx.op == "readdir":
            routes = set(ctx.resolution.children)  # Child routes have their own chain
            return [
                e if e.name in routes else await self._entry(ctx, backend, real / e.name, e)
                for e in result
            ]
        if not self.matches(real.name):
            return result
        if ctx.op == "open":
            _, data = await self._output(ctx, backend, real, None)
            if data is None:
                raise FsError(errno.EFBIG, f"larger than {self.max_bytes} bytes")
            return Opened(data=data, direct_io=True)
        attr: Attr = result
        return await self._sized(ctx, backend, real, attr)

    async def _entry(self, ctx: Context, backend: BackendView, real: Path, e: DirEntry) -> DirEntry:
        if not self.matches(e.name):
            return e
        return DirEntry(e.name, await self._sized(ctx, backend, real, e.attr))

    async def _sized(self, ctx: Context, backend: BackendView, real: Path, attr: Attr) -> Attr:
        if not stat.S_ISREG(attr.st_mode):
            return attr
        try:
            raw, data = await self._output(ctx, backend, real, attr)
        except FsError:
            # Gone or grown past max_bytes since the listing: what the backend said is the
            # best answer left.
            return attr
        if data is None:
            return raw  # Too large: its raw size, and EFBIG on open
        return replace(attr, st_size=len(data), st_blocks=(len(data) + 511) // 512)

    async def _output(
        self, ctx: Context, backend: BackendView, real: Path, seen: Attr | None
    ) -> tuple[Attr, bytes | None]:
        """The file's raw attributes, and `fn` over its content or over an inner transform's
        output: `None` if the file, or that output, is larger than `max_bytes`.

        The innermost transform records the raw attributes in `ctx.state`, so every transform
        in a chain keys its cache, and checks its limit, on the source file itself.
        """
        runs: dict[Path, tuple[Attr, bytes | None]] = ctx.state.setdefault(_STATE, {})
        source: bytes | None = None
        if real in runs:
            raw, source = runs[real]
            if source is None:
                return raw, None  # An inner transform refused it
        else:
            raw = seen if seen is not None else await backend.lstat(real)
        if raw.st_size > self.max_bytes or (source is not None and len(source) > self.max_bytes):
            runs[real] = (raw, None)
            return raw, None
        key = (real, raw.st_mtime_ns, raw.st_size)
        out = self._cache.get(key)
        if out is None:
            if source is None:
                source = await backend.read_all(real, self.max_bytes)
            out = await asyncio.to_thread(self.fn, source)
            self._remember(key, out)
        else:
            self._cache.move_to_end(key)
        runs[real] = (raw, out)
        return raw, out

    def _remember(self, key: tuple[Path, int, int], out: bytes) -> None:
        # Two calls can miss on one key at once: both ran fn, and one copy is kept.
        if len(out) > self.cache_bytes or key in self._cache:
            return
        self._cache[key] = out
        self._cached += len(out)
        while self._cached > self.cache_bytes:
            _, dropped = self._cache.popitem(last=False)
            self._cached -= len(dropped)

    def __repr__(self) -> str:
        return f"Transform({getattr(self.fn, '__qualname__', self.fn)!s}, match={self.match!r})"
