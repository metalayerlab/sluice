"""ShadowFS-style rules as middleware: hide files by pattern, size or age.

An example, not part of the sluice package: copy it beside your app module and adapt it.
A hidden entry is dropped from listings and is `ENOENT` to every path op, so a listing and a
lookup agree. Creating a hidden name (or renaming onto one) is `EACCES`: it would vanish.

    from hide import Hide, glob, larger_than, older_than

    app.route("/share", backend="/srv/share", middlewares=[
        Hide(glob(".*"), glob("*.tmp"), glob("build/**"), larger_than(1 << 30)),
    ])

A rule is any `(rel, attr) -> bool`: `rel` is the path below the backend, `attr` its `lstat`
(`None` for a name that doesn't exist yet). A hidden directory hides what is under it too: the
kernel looks each component up on the way down. Child routes are never hidden: their own chain
decides.

Limits, kept for brevity: hidden entries still exist, so `rmdir` of a directory that lists as
empty is `ENOTEMPTY`; and only the renamed name is checked, so renaming a directory to `out`
under `glob("out/**")` makes its contents vanish.
"""

from __future__ import annotations

import errno
import fnmatch
import re
import stat
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from sluice import Attr, Context, DirEntry, FsError, Middleware

type Rule = Callable[[PurePosixPath, Attr | None], bool]

# Ops on a path. Handle ops (read, write, flush, fsync) need no check: only an open this
# middleware allowed made the handle.
_PATH_OPS = frozenset(
    {"lookup", "getattr", "setattr", "readlink", "mkdir", "rmdir", "unlink", "symlink"}
    | {"rename", "open", "create", "opendir", "access"}
    | {"getxattr", "setxattr", "listxattr", "removexattr"}
)
_CREATES = frozenset({"mkdir", "symlink", "create"})


def glob(pattern: str) -> Rule:
    """Match by name (`*.tmp`), or by path below the backend when `pattern` has a `/`
    (`build/**`: everything under `build`). `**` spans directories, `**/` zero or more of them,
    and `*`, `?` and `[...]` stay within one name, as in `fnmatch`."""
    if "/" not in pattern:
        return lambda rel, attr: fnmatch.fnmatchcase(rel.name, pattern)
    regex = re.compile(_translate(pattern.strip("/")))
    return lambda rel, attr: regex.fullmatch(rel.as_posix()) is not None


def _translate(pattern: str) -> str:
    out = []
    wild = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    for part in re.split(r"(\*\*/|\*\*|\*|\?|\[!?\]?[^]]*\])", pattern):
        if part.startswith("[") and part.endswith("]") and len(part) > 2:
            body = part[1:-1]
            negate = body.startswith("!")
            body = re.escape(body[1:] if negate else body).replace("\\-", "-")
            out.append(f"[^/{body}]" if negate else f"(?!/)[{body}]")
        else:
            out.append(wild.get(part, re.escape(part)))
    return "".join(out)


def larger_than(size: int) -> Rule:
    """Files of more than `size` bytes. Directories are never hidden by size."""
    return lambda rel, attr: attr is not None and attr.st_size > size and not _is_dir(attr)


def older_than(seconds: float, clock: Callable[[], float] = time.time) -> Rule:
    """Files last modified more than `seconds` ago."""

    def rule(rel: PurePosixPath, attr: Attr | None) -> bool:
        if attr is None or _is_dir(attr):
            return False
        return attr.st_mtime_ns < (clock() - seconds) * 1e9

    return rule


def _is_dir(attr: Attr) -> bool:
    return stat.S_ISDIR(attr.st_mode)


class Hide(Middleware):
    """Hide every entry any of `rules` matches."""

    ops = _PATH_OPS | {"readdir"}

    def __init__(self, *rules: Rule) -> None:
        self.rules = rules

    def hidden(self, rel: PurePosixPath, attr: Attr | None) -> bool:
        return any(rule(rel, attr) for rule in self.rules)

    async def pre(self, ctx: Context) -> None:
        backend, real = ctx.backend, ctx.real_path
        if ctx.op == "readdir" or backend is None or real is None or real == backend.root:
            return
        try:
            attr: Attr | None = await backend.lstat(real)
        except FsError as e:
            if e.errno != errno.ENOENT:
                raise
            attr = None
        if not self.hidden(PurePosixPath(real.relative_to(backend.root)), attr):
            return
        # Making a hidden name, or renaming onto one, would succeed and then vanish.
        target = ctx.op == "rename" and ctx.path == ctx.args.target
        if ctx.op in _CREATES or target:
            raise FsError(errno.EACCES, "hidden name")
        raise FsError(errno.ENOENT)

    async def post(self, ctx: Context, result: Any) -> Any:
        if ctx.op != "readdir" or ctx.backend is None or ctx.real_path is None:
            return result
        here = PurePosixPath(ctx.real_path.relative_to(ctx.backend.root))
        entries: list[DirEntry] = result
        routes = set(ctx.resolution.children)  # Their own chain decides, as their lookups do
        return [e for e in entries if e.name in routes or not self.hidden(here / e.name, e.attr)]

    def __repr__(self) -> str:
        n = len(self.rules)
        return f"Hide({n} rule{'' if n == 1 else 's'})"
