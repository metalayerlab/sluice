"""YAML projection: one YAML document mounted as a directory tree, read-only (design 0011).

An example, not part of the sluice package: copy it beside your app module and adapt it.
Mappings are directories, scalars are files and a list of scalars is one file, one item per
line, so `ls` and `cat` read a structured record one property at a time:

    from yaml_projection import DocumentStore, Projection

    store = DocumentStore("/srv/records/widget.yaml", MyModel())  # Refuses a bad document
    app.route("/records/widget/{rest*}", virtual=True, handler=Projection(store))

The projector never parses YAML itself. A `Model` loads, validates and types the document, so
the projector can't disagree with it about what the document means (design 0011 "YAML
library"). The document must validate when it is mounted, and must hold no list of mappings or
of lists anywhere. The store reloads it at `opendir` when the file's inode, size or mtime has
changed, so a rename-replace is seen; a replacement that no longer validates is logged and the
last good document is kept.

A leaf's content is its value's text and one newline (design 0011 "Scalar serialization"):
null is an empty file, a float is its `repr`, dates and datetimes are ISO 8601, and the declared
type decides, else the current value's type. A leaf the model marks `is_reference` (a `pass:`
path, say) reads as its reference text: nothing resolves it, and nothing here runs `pass` or
any other subprocess.

This step is read-only: every leaf is mode 0444, and opening for writing, `truncate`,
`setattr` and every shape change (`create`, `mkdir`, `unlink`, `rmdir`, `symlink`) fail
with `EROFS`; sluice refuses a `rename` on a virtual route with `EXDEV` before any handler.
A reference leaf fails opening for writing, `truncate` and `setattr` with `EACCES`, checked
before `writable` is ever consulted. `dump` and `writable` are part of `Model` for the write
step, and unused here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import errno
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from sluice import Attr, Context, DirEntry, FsError, Opened, VirtualHandler

log = logging.getLogger(__name__)

# The declared types of design 0011. A float has none: it is typed by its current value.
type Type = Literal["string", "integer", "boolean", "date", "datetime", "list", "mapping"]
type Finding = tuple[str, str, str]  # (code, JSON Pointer, message)
type Scalar = str | int | float | bool | dt.date | dt.datetime | None


class Model(Protocol):
    """What the projector needs to know about one kind of document. Pointers are JSON Pointers."""

    def load(self, path: Path) -> dict[str, Any]:
        """The document at `path`, unvalidated. May raise; the message must not quote the file."""
        ...

    def dump(self, doc: dict[str, Any]) -> str:
        """`doc` as YAML text that `load` reads back equal. Unused until writes."""
        ...

    def validate(self, doc: dict[str, Any]) -> list[Finding]:
        """Every finding, as (code, pointer, message); empty means valid. Never raises."""
        ...

    def type_of(self, pointer: str, doc: dict[str, Any]) -> Type | None:
        """The declared type at `pointer`, or `None` to use the current value's type."""
        ...

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        """Whether the leaf at `pointer` may be written. Unused until writes."""
        ...

    def is_reference(self, pointer: str, doc: dict[str, Any]) -> bool:
        """Whether the leaf at `pointer` names a secret held elsewhere. It is never resolved."""
        ...


class DocumentError(ValueError):
    """The document can't be mounted: it has findings, or a shape the projection can't show."""

    def __init__(self, path: Path, findings: list[Finding]) -> None:
        super().__init__(
            f"{path}: {len(findings)} finding(s), first {findings[0][0]} at {findings[0][1] or '/'}"
        )
        self.path = path
        self.findings = findings


def pointer(parts: tuple[str, ...]) -> str:
    """The JSON Pointer (RFC 6901) for a path of keys: `("a/b", "c")` is `/a~1b/c`."""
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _shape(node: Any, at: tuple[str, ...] = ()) -> Iterator[Finding]:
    """What the projection can't show: odd keys, lists of containers, values YAML can hold
    but no declared type covers (`!!binary`, `!!set`)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str) or key in {"", ".", ".."} or "/" in key or "\0" in key:
                yield ("key", pointer(at), f"key {key!r} can't be a file name")
            else:
                yield from _shape(value, (*at, key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, dict | list):
                yield ("list-of-containers", pointer((*at, str(i))), "a list may hold scalars only")
            elif not _is_scalar(item):
                yield ("value", pointer((*at, str(i))), f"a {type(item).__name__} isn't a scalar")
    elif not _is_scalar(node):
        yield ("value", pointer(at), f"a {type(node).__name__} isn't a scalar")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | dt.date)


@dataclass(frozen=True)
class _Stamp:
    """What says the file changed: a rename-replace changes the inode, an edit size or mtime."""

    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: Path) -> _Stamp:
        st = os.stat(path)
        return cls(st.st_ino, st.st_size, st.st_mtime_ns)


class DocumentStore:
    """One document: loaded through its model, checked, held in memory, reloaded on change.

    The constructor loads and checks the document, and raises `DocumentError` (or the model's
    own load error) if it can't be mounted. Reads see `doc`, which a reload replaces in one
    assignment, so a read sees the old document or the new one, never half of each.
    """

    def __init__(self, path: str | Path, model: Model) -> None:
        self.path = Path(path)
        self.model = model
        self._seen = _Stamp.of(self.path)
        self.doc = self._checked(model.load(self.path))
        self.mtime_ns = self._seen.mtime_ns
        self.lock = asyncio.Lock()  # Serialises reloads now, and commits once writes come

    def _checked(self, doc: Any) -> dict[str, Any]:
        findings: list[Finding] = [("not-a-mapping", "", "the document isn't a mapping")]
        if isinstance(doc, dict):
            # The model validates only a shape the projection can show
            findings = list(_shape(doc)) or self.model.validate(doc)
        for code, at, message in findings:
            log.warning("yaml_projection: %s: %s at %s: %s", self.path, code, at or "/", message)
        if findings or not isinstance(doc, dict):
            raise DocumentError(self.path, findings)
        return doc

    async def refresh(self) -> None:
        """Reload if the file changed since it was last seen. Never raises: a document that
        no longer loads or validates is logged at warning, and the last good one stays."""
        async with self.lock:
            await asyncio.to_thread(self._refresh)

    def _refresh(self) -> None:
        try:
            stamp = _Stamp.of(self.path)
        except OSError as e:
            log.warning(
                "yaml_projection: %s: %s; keeping the last good document", self.path, e.strerror
            )
            return
        if stamp == self._seen:
            return
        self._seen = stamp  # A bad replacement is reported once, not at every opendir
        try:
            doc = self._checked(self.model.load(self.path))
        except DocumentError:
            log.warning(
                "yaml_projection: %s changed and doesn't validate; keeping the last good document",
                self.path,
            )
            return
        except Exception as e:  # The model's load error: its type is the model's own
            log.warning(
                "yaml_projection: %s changed and doesn't load (%s); keeping the last good document",
                self.path,
                type(e).__name__,
            )
            return
        self.doc, self.mtime_ns = doc, stamp.mtime_ns
        log.info("yaml_projection: reloaded %s", self.path)


def render(value: Scalar | list[Scalar], declared: Type | None = None) -> bytes:
    """A leaf's content: each scalar's text and one newline; null is empty."""
    if isinstance(value, list) and declared in {None, "list"}:
        # Each item is a line, a null one empty. Items have no declared type in v1.
        return "".join(
            ("" if item is None else _text(item, None)) + "\n" for item in value
        ).encode()
    if value is None:
        return b""
    return (_text(value, declared) + "\n").encode()


def _text(value: Scalar | list[Scalar], declared: Type | None) -> str:
    kind = declared or _kind(value)
    match kind:
        case "string" if isinstance(value, str):
            return value
        case "boolean" if isinstance(value, bool):
            return "true" if value else "false"
        case "integer" if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        case "float" if isinstance(value, float):
            return repr(value)
        case "datetime" if isinstance(value, dt.datetime):
            return value.isoformat()
        case "date" if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value.isoformat()
    # The model validated the document, so this is its bug: say so rather than guess.
    log.warning("yaml_projection: a %s value where %s is declared", type(value).__name__, kind)
    raise FsError(errno.EIO, f"value doesn't match its declared type {kind}")


def _kind(value: Scalar | list[Scalar]) -> str:
    if isinstance(value, bool):  # Before int: a bool is an int to Python
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, dt.datetime):  # Before date: a datetime is a date to Python
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    return "string"


class Projection(VirtualHandler):
    """A `{rest*}` route's handler: the store's document as a read-only tree."""

    def __init__(self, store: DocumentStore, *, param: str = "rest") -> None:
        self.store = store
        self.param = param

    def _node(self, ctx: Context) -> tuple[tuple[str, ...], Any, dict[str, Any]]:
        """The keys to the node at this path, the node, and the document it is in."""
        rest = ctx.params.get(self.param, "") or ""
        parts = tuple(p for p in PurePosixPath(rest).parts if p not in {"/", "."})
        doc = self.store.doc
        node: Any = doc
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise FsError(errno.ENOENT)
            node = node[part]
        return parts, node, doc

    def _content(self, parts: tuple[str, ...], node: Any, doc: dict[str, Any]) -> bytes:
        # A reference reads as its text by the same rule: nothing here resolves it.
        return render(node, self.store.model.type_of(pointer(parts), doc))

    def _attr(self, parts: tuple[str, ...], node: Any, doc: dict[str, Any]) -> Attr:
        mtime = self.store.mtime_ns
        if isinstance(node, dict):
            return Attr.directory(0o555, mtime_ns=mtime)
        size = len(self._content(parts, node, doc))
        return Attr.file(0o444, size=size, mtime_ns=mtime)  # Every leaf, in the read-only step

    def _refuse_change(self, parts: tuple[str, ...], doc: dict[str, Any]) -> FsError:
        # The reference check comes first, and in this step `writable` is never consulted.
        if self.store.model.is_reference(pointer(parts), doc):
            return FsError(errno.EACCES, "a secret reference is never writable")
        return FsError(errno.EROFS, "the YAML projection is read-only")

    async def getattr(self, ctx: Context) -> Attr:
        return self._attr(*self._node(ctx))

    async def setattr(self, ctx: Context) -> Attr:
        parts, _, doc = self._node(ctx)
        raise self._refuse_change(parts, doc)

    async def open(self, ctx: Context) -> Opened:
        parts, node, doc = self._node(ctx)
        if isinstance(node, dict):
            raise FsError(errno.EISDIR)
        flags: int = ctx.args.flags
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & os.O_TRUNC:
            raise self._refuse_change(parts, doc)
        return Opened(data=self._content(parts, node, doc), direct_io=True)

    async def opendir(self, ctx: Context) -> Opened:
        await self.store.refresh()  # Design 0011, question 2: reads reload at opendir
        _, node, _ = self._node(ctx)
        if not isinstance(node, dict):
            raise FsError(errno.ENOTDIR)
        return Opened()

    async def readdir(self, ctx: Context, handle: Opened) -> list[DirEntry]:
        parts, node, doc = self._node(ctx)
        if not isinstance(node, dict):
            raise FsError(errno.ENOTDIR)
        return [DirEntry(key, self._attr((*parts, key), value, doc)) for key, value in node.items()]

    def __repr__(self) -> str:
        return f"Projection({str(self.store.path)!r})"
