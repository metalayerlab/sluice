"""YAML projection: one YAML document mounted as a tree, with checked writes (design 0011).

An example, not part of the sluice package: copy it beside your app module and adapt it.
Mappings are directories, scalars are files and a list of scalars is one file, one item per
line, so `ls` and `cat` read a structured record one property at a time, and `echo`, `printf`
or an editor change one value:

    from yaml_projection import DocumentStore, Projection

    store = DocumentStore("/srv/records/widget.yaml", MyModel())  # Refuses a bad document
    app.route("/records/widget/{rest*}", virtual=True, handler=Projection(store))

The projector never parses YAML itself. A `Model` loads, dumps, validates and types the
document, so the projector can't disagree with it about what the document means (design 0011
"YAML library"). The document must validate when it is mounted, and must hold no list of
mappings or of lists anywhere. The store reloads it at `opendir` when the file's inode, size or
mtime has changed, so a rename-replace is seen; a replacement that no longer validates is
logged and the last good document is kept.

A leaf's content is its value's text and one newline (design 0011 "Scalar serialization"):
null is an empty file, a float is its `repr`, dates and datetimes are ISO 8601, and the declared
type decides, else the current value's type. A leaf the model marks `is_reference` (a `pass:`
path, say) reads as its reference text: nothing resolves it, and nothing here runs `pass` or
any other subprocess.

Writes (design 0011 "A write, step by step"). A leaf the model marks `writable` is mode 0644,
every other leaf 0444. Opening one for writing gives a handle with a buffer, empty with
`O_TRUNC`, else the current content; `write` changes the buffer, and a write at offset 0 (what
`echo` and `printf` send) casts and validates the buffer as it stands, failing that `write`
with `EINVAL`, so the shell sees it. `flush` and `fsync` commit, under the store's lock: cast,
set the leaf in a deep copy, validate the copy, `dump` it to a temporary file beside the
document, `load` that back and require it equal to the copy, fsync, rename it over the
document, and swap the copy in. The blocking steps run in a worker thread. Any failure leaves
the document on disk and in memory as it was, and removes the temporary file. A handle that
wrote nothing commits nothing; `release` only drops the buffer.

A write casts strictly: one trailing newline is stripped, an integer is `-?[0-9]+` with no `+`,
`_` or leading zero, a boolean is `true` or `false`, a date `YYYY-MM-DD`, a datetime what
`datetime.fromisoformat` reads, a float (a leaf with no declared type holding one) what
`float()` reads, finite. Zero bytes are null, which only a leaf whose current value is null
(and no declared type) takes. A list is replaced whole, one string item per line.

What a writer sees, and why (each refusal is logged at warning with its code and JSON Pointer,
never the value):

- `EINVAL`: the content doesn't cast to the leaf's type, isn't UTF-8, or the model's
  `validate` has a finding for the changed document;
- `EACCES`: the leaf isn't writable, or is a secret reference (checked before `writable` is
  ever consulted, at `open` and at `setattr`);
- `EBUSY`: the file changed on disk since it was loaded (another writer): it is reloaded and
  left as the other writer left it; retry;
- `EIO`: the model's `dump` doesn't load back equal, or fails;
- `EPERM`: a shape change (`create`, `mkdir`, `unlink`, `rmdir`, `symlink`), any `setattr` on
  a directory, or a mode or owner change on a leaf. Times are accepted and ignored, and a size
  change is a truncate. sluice refuses a `rename` on a virtual route with `EXDEV` before any
  handler.

`truncate(2)` on a leaf with no write handle open commits the shortened content as a write
would. While a write handle on that leaf is open, a size change goes to the open buffers
instead, committed at their `flush`: the handler can't tell `ftruncate` on a handle from
`truncate` on the path, and an editor truncates the file it has open.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import errno
import logging
import math
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from sluice import Attr, Context, DirEntry, FsError, Opened, VirtualHandler

log = logging.getLogger(__name__)

# The declared types of design 0011. A float has none: it is typed by its current value.
type Type = Literal["string", "integer", "boolean", "date", "datetime", "list", "mapping"]
type Finding = tuple[str, str, str]  # (code, JSON Pointer, message)
type Scalar = str | int | float | bool | dt.date | dt.datetime | None

_INTEGER = re.compile(r"0|-?[1-9][0-9]*")  # No `+`, `_`, leading zero or `-0`
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Model(Protocol):
    """What the projector needs to know about one kind of document. Pointers are JSON Pointers."""

    def load(self, path: Path) -> dict[str, Any]:
        """The document at `path`, unvalidated. May raise; the message must not quote the file."""
        ...

    def dump(self, doc: dict[str, Any]) -> str:
        """`doc` as YAML text that `load` reads back equal."""
        ...

    def validate(self, doc: dict[str, Any]) -> list[Finding]:
        """Every finding, as (code, pointer, message); empty means valid. Never raises."""
        ...

    def type_of(self, pointer: str, doc: dict[str, Any]) -> Type | None:
        """The declared type at `pointer`, or `None` to use the current value's type."""
        ...

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        """Whether the leaf at `pointer` may be written. Never asked about a reference."""
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
    """One document: loaded through its model, checked, held in memory, reloaded on change,
    and written back atomically.

    The constructor loads and checks the document, and raises `DocumentError` (or the model's
    own load error) if it can't be mounted. Reads see `doc`, which a reload or a commit
    replaces in one assignment, so a read sees the old document or the new one, never half of
    each. `lock` serialises reloads and commits.
    """

    def __init__(self, path: str | Path, model: Model) -> None:
        self.path = Path(path)
        self.model = model
        self._seen = _Stamp.of(self.path)
        self.doc = self._checked(model.load(self.path))
        self._loaded = self._seen  # The file `doc` came from: a commit refuses any other
        self.mtime_ns = self._seen.mtime_ns
        self.lock = asyncio.Lock()

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
        self.doc, self._loaded, self.mtime_ns = doc, stamp, stamp.mtime_ns
        log.info("yaml_projection: reloaded %s", self.path)

    # Writes

    def _refuse(self, at: str, code: str, message: str, err: int = errno.EINVAL) -> FsError:
        """Log why a write is refused (design 0011, question 3): never the value."""
        log.warning(
            "yaml_projection: %s: write refused: %s at %s: %s", self.path, code, at or "/", message
        )
        return FsError(err, message)

    def candidate(self, parts: tuple[str, ...], content: bytes) -> dict[str, Any]:
        """The document with the leaf at `parts` set to `content`, cast and validated.

        Raises `FsError`: `EINVAL` for a bad cast or a finding, `EACCES` for a leaf that
        isn't writable (now), `ENOENT` or `EISDIR` for one the document no longer has.
        Blocking: run it in a worker thread.
        """
        doc, at = self.doc, pointer(parts)
        node: Any = doc
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                raise FsError(errno.ENOENT, "the leaf is no longer in the document")
            node = node[part]
        if isinstance(node, dict) or not parts:
            raise FsError(errno.EISDIR)
        if self.model.is_reference(at, doc):  # Before `writable`, always
            raise self._refuse(
                at, "reference", "a secret reference is never writable", errno.EACCES
            )
        if not self.model.writable(at, doc):
            raise self._refuse(at, "read-only", "the model doesn't allow writing it", errno.EACCES)
        try:
            value = cast(content, self.model.type_of(at, doc), node)
        except ValueError as e:
            raise self._refuse(at, "cast", str(e)) from None
        new = copy.deepcopy(doc)
        parent: Any = new
        for part in parts[:-1]:
            parent = parent[part]
        parent[parts[-1]] = value
        findings = list(_shape(new)) or self.model.validate(new)
        for code, where, message in findings:
            self._refuse(where, code, message)
        if findings:
            raise FsError(errno.EINVAL, f"{len(findings)} finding(s), first at {findings[0][1]}")
        return new

    async def check(self, parts: tuple[str, ...], content: bytes) -> None:
        """Raise what committing `content` now would raise at the cast or validation."""
        await asyncio.to_thread(self.candidate, parts, content)

    async def commit(self, parts: tuple[str, ...], content: bytes) -> None:
        """Set the leaf at `parts` to `content` on disk and in memory, or change nothing."""
        async with self.lock:
            new, stamp = await asyncio.to_thread(self._commit, parts, content)
            self.doc, self._seen, self._loaded, self.mtime_ns = new, stamp, stamp, stamp.mtime_ns

    def _unchanged(self) -> None:
        """Refuse with `EBUSY` if another writer changed the file since `doc` was loaded
        (design 0011, question 2): reload, and leave the file as that writer left it."""
        try:
            changed = _Stamp.of(self.path) != self._loaded
        except OSError:
            changed = True
        if changed:
            self._refresh()
            raise self._refuse(
                "", "changed", "the file changed on disk since it was loaded", errno.EBUSY
            )

    def _commit(self, parts: tuple[str, ...], content: bytes) -> tuple[dict[str, Any], _Stamp]:
        self._unchanged()
        new = self.candidate(parts, content)
        try:
            text = self.model.dump(new)
        except Exception as e:  # The model's own error type; its message may quote the value
            raise self._refuse("", "dump", f"dump failed ({type(e).__name__})", errno.EIO) from None
        directory = self.path.parent
        try:
            fd, name = tempfile.mkstemp(dir=directory, prefix=f".{self.path.name}.", suffix=".tmp")
        except OSError as e:  # A directory it can't write in, say
            raise self._refuse(
                "", "io", e.strerror or "write-back failed", e.errno or errno.EIO
            ) from None
        tmp, renamed = Path(name), False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), stat.S_IMODE(os.stat(self.path).st_mode))
                f.write(text)
                f.flush()
                self._loads_back(tmp, new)
                os.fsync(f.fileno())
            self._unchanged()  # Again, as late as it can be: dump and load-back take time
            os.replace(tmp, self.path)
            renamed = True
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)  # The rename itself survives a crash
            finally:
                os.close(dir_fd)
            return new, _Stamp.of(self.path)
        except OSError as e:
            if renamed:  # Only the directory fsync can fail here: the new file is in place
                log.warning("yaml_projection: %s: fsync of its directory failed", self.path)
                return new, _Stamp.of(self.path)
            raise self._refuse(
                "", "io", e.strerror or "write-back failed", e.errno or errno.EIO
            ) from None
        finally:
            if not renamed:
                with suppress(FileNotFoundError):
                    tmp.unlink()

    def _loads_back(self, tmp: Path, new: dict[str, Any]) -> None:
        try:
            back = self.model.load(tmp)
        except Exception as e:  # The model's load error: its type is the model's own
            raise self._refuse(
                "", "load-back", f"doesn't load ({type(e).__name__})", errno.EIO
            ) from None
        if back != new:
            raise self._refuse("", "load-back", "dump doesn't load back equal", errno.EIO)


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


def cast(content: bytes, declared: Type | None, current: Any) -> Scalar | list[Scalar]:
    """A leaf's new value from what was written to it (design 0011 "Scalar serialization").

    The declared type decides, else the current value's type: a null leaf takes only null (0
    bytes), a list only a list. Raises `ValueError` with a message that never quotes the
    content.
    """
    kind: str = declared or _kind(current)
    if not declared and current is None:
        kind = "null"
    elif not declared and isinstance(current, list):
        kind = "list"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not valid UTF-8") from None
    if kind == "list":
        # One item per line, so no item can hold a newline. Items are strings in v1.
        items: list[Scalar] = [*text.removesuffix("\n").split("\n")] if content else []
        return items
    if not content:
        if kind == "null":
            return None
        raise ValueError(f"an empty file is null, which a {kind} leaf can't hold")
    text = text.removesuffix("\n")  # One newline: `echo` adds it, and a read shows it
    match kind:
        case "string":
            return text
        case "integer" if _INTEGER.fullmatch(text):
            with suppress(ValueError):  # Past Python's digit limit
                return int(text)
        case "boolean" if text in {"true", "false"}:
            return text == "true"
        case "date" if _DATE.fullmatch(text):
            with suppress(ValueError):
                return dt.date.fromisoformat(text)
        case "datetime":
            with suppress(ValueError):
                return dt.datetime.fromisoformat(text)
        case "float":
            with suppress(ValueError):
                number = float(text)
                if math.isfinite(number):
                    return number
        case "null":
            raise ValueError("a null leaf takes only an empty file")
    raise ValueError(f"isn't a valid {kind}")


class _Buffer:
    """One write handle's content: what `flush` commits, if it was written to."""

    def __init__(self, parts: tuple[str, ...], data: bytes) -> None:
        self.parts = parts
        self.data = bytearray(data)
        self.dirty = False

    def resize(self, size: int) -> None:
        del self.data[size:]
        self.data.extend(b"\0" * (size - len(self.data)))
        self.dirty = True


@dataclass(frozen=True)
class _Writing(Opened):
    """A write handle: the buffer is mutable, the handle (as sluice keeps it) isn't."""

    buffer: _Buffer = field(default_factory=lambda: _Buffer((), b""))


class Projection(VirtualHandler):
    """A `{rest*}` route's handler: the store's document as a tree, writable where the model
    says so."""

    def __init__(self, store: DocumentStore, *, param: str = "rest") -> None:
        self.store = store
        self.param = param
        self._writers: dict[tuple[str, ...], set[_Buffer]] = {}  # Open write buffers, by leaf

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

    def _refusal(self, parts: tuple[str, ...], doc: dict[str, Any]) -> FsError | None:
        """Why the leaf at `parts` can't be written, or `None`. A reference is checked first,
        so `writable` is never asked about one."""
        at = pointer(parts)
        if self.store.model.is_reference(at, doc):
            return FsError(errno.EACCES, "a secret reference is never writable")
        if not self.store.model.writable(at, doc):
            return FsError(errno.EACCES, "this leaf is read-only")
        return None

    def _attr(self, parts: tuple[str, ...], node: Any, doc: dict[str, Any]) -> Attr:
        mtime = self.store.mtime_ns
        if isinstance(node, dict):
            return Attr.directory(0o555, mtime_ns=mtime)
        size = len(self._content(parts, node, doc))
        mode = 0o444 if self._refusal(parts, doc) else 0o644
        return Attr.file(mode, size=size, mtime_ns=mtime)

    @staticmethod
    def _buffer(handle: Opened) -> _Buffer:
        if not isinstance(handle, _Writing):
            raise FsError(errno.EBADF, "not open for writing")
        return handle.buffer

    async def getattr(self, ctx: Context) -> Attr:
        return self._attr(*self._node(ctx))

    async def setattr(self, ctx: Context) -> Attr:
        parts, node, doc = self._node(ctx)
        if isinstance(node, dict):
            raise FsError(errno.EPERM, "a directory's attributes can't change")
        refused = self._refusal(parts, doc)
        if refused:
            raise refused
        args = ctx.args
        if args.mode is not None or args.uid is not None or args.gid is not None:
            raise FsError(errno.EPERM, "a leaf's mode and owner can't change")
        if args.size is not None:  # Times are accepted and ignored (design 0011, question 4)
            writers = self._writers.get(parts)
            if writers:  # An open handle's truncate: committed at its flush
                for buffer in writers:
                    buffer.resize(args.size)
            else:
                data = bytearray(self._content(parts, node, doc))
                data[args.size :] = b""
                data.extend(b"\0" * (args.size - len(data)))
                await self.store.commit(parts, bytes(data))
        return self._attr(*self._node(ctx))

    async def open(self, ctx: Context) -> Opened:
        parts, node, doc = self._node(ctx)
        if isinstance(node, dict):
            raise FsError(errno.EISDIR)
        flags: int = ctx.args.flags
        if flags & os.O_ACCMODE == os.O_RDONLY and not flags & os.O_TRUNC:
            return Opened(data=self._content(parts, node, doc), direct_io=True)
        refused = self._refusal(parts, doc)
        if refused:
            raise refused
        data = b"" if flags & os.O_TRUNC else self._content(parts, node, doc)
        buffer = _Buffer(parts, data)
        self._writers.setdefault(parts, set()).add(buffer)
        return _Writing(data=data, direct_io=True, buffer=buffer)

    async def read(self, ctx: Context, handle: Opened) -> bytes:
        data = handle.buffer.data if isinstance(handle, _Writing) else handle.data
        offset: int = ctx.args.offset
        return bytes(data[offset : offset + ctx.args.size])

    async def write(self, ctx: Context, handle: Opened) -> int:
        buffer = self._buffer(handle)
        offset: int = ctx.args.offset
        data: bytes = ctx.args.data
        if offset > len(buffer.data):
            buffer.data.extend(b"\0" * (offset - len(buffer.data)))
        buffer.data[offset : offset + len(data)] = data
        buffer.dirty = True
        if offset == 0:  # `echo` and `printf`: the shell ignores close's error, not write's
            await self.store.check(buffer.parts, bytes(buffer.data))
        return len(data)

    async def flush(self, ctx: Context, handle: Opened) -> None:
        if not isinstance(handle, _Writing) or not handle.buffer.dirty:
            return  # A handle that wrote nothing commits nothing
        await self.store.commit(handle.buffer.parts, bytes(handle.buffer.data))
        handle.buffer.dirty = False  # A second flush (a dup'd fd's close) commits nothing

    async def fsync(self, ctx: Context, handle: Opened) -> None:
        await self.flush(ctx, handle)

    async def release(self, handle: Opened) -> None:
        if isinstance(handle, _Writing):  # Only drops the buffer: a commit is flush's
            writers = self._writers.get(handle.buffer.parts, set())
            writers.discard(handle.buffer)
            if not writers:
                self._writers.pop(handle.buffer.parts, None)

    async def access(self, ctx: Context) -> bool:
        if not ctx.args.mode & os.W_OK:
            return True
        parts, node, doc = self._node(ctx)
        return not isinstance(node, dict) and self._refusal(parts, doc) is None

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

    # The pilot changes values, not the document's shape (design 0011, "Writes")

    async def create(self, ctx: Context) -> tuple[Opened, Attr]:
        raise FsError(errno.EPERM, "the projection's shape can't change")

    async def mkdir(self, ctx: Context) -> Attr:
        raise FsError(errno.EPERM, "the projection's shape can't change")

    async def rmdir(self, ctx: Context) -> None:
        raise FsError(errno.EPERM, "the projection's shape can't change")

    async def unlink(self, ctx: Context) -> None:
        raise FsError(errno.EPERM, "the projection's shape can't change")

    async def symlink(self, ctx: Context) -> Attr:
        raise FsError(errno.EPERM, "the projection's shape can't change")

    def __repr__(self) -> str:
        return f"Projection({str(self.store.path)!r})"
