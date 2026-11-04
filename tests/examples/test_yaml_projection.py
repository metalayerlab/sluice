"""The generic YAML projector (design 0011, steps 2 and 4) on a toy document and model."""

import asyncio
import datetime as dt
import errno
import logging
import os
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import pytest
import yaml
from yaml_projection import (
    DocumentError,
    DocumentStore,
    Finding,
    Model,
    Projection,
    Type,
    cast,
    pointer,
)

from sluice import FsError, Sluice
from sluice.context import SetattrArgs
from sluice.dispatch import Dispatcher
from sluice.mount import MAX_WRITE_FLOOR

from .conftest import cat, errno_of, ls, run, walk

type Mount = Callable[[Sluice], Dispatcher]

TOY = """\
name: Example Widget Co
motto: 'true'
summary: >-
  Makes widgets
  and sprockets.
staff: 12
active: true
ratio: 0.5
notes: null
founded: 2001-04-01
updated: 2026-11-04T09:30:00Z
tags: [alpha, beta, 3, null, 2.5]
empty: []
counts: [1, 2, 3]
address:
  street: 1 High Street
  city: Exampleton
  geo:
    lat: 51.5
contact:
  vault: pass:services/example/contact
"""


class ToyModel:
    """A sluice-owned model: pyyaml for load and dump, a few declared types, one reference."""

    TYPES: ClassVar[dict[str, Type]] = {
        "/name": "string",
        "/motto": "string",
        "/staff": "integer",
        "/active": "boolean",
        "/founded": "date",
        "/updated": "datetime",
        "/tags": "list",
        "/address": "mapping",
    }
    REFERENCES: ClassVar[set[str]] = {"/contact/vault"}
    READ_ONLY: ClassVar[set[str]] = {"/name"}

    def __init__(self) -> None:
        self.writable_calls: list[str] = []
        self.dumps = 0
        self.lossy = False  # Set: `dump` loses a value, so the load-back check must catch it

    def load(self, path: Path) -> dict[str, Any]:
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return doc

    def dump(self, doc: dict[str, Any]) -> str:
        self.dumps += 1
        if self.lossy:
            doc = {**doc, "motto": "lost"}
        return yaml.safe_dump(doc, sort_keys=False)

    def validate(self, doc: dict[str, Any]) -> list[Finding]:
        found: list[Finding] = []
        if not isinstance(doc.get("name"), str):
            found.append(("required", "/name", "name must be a string"))
        if not isinstance(doc.get("staff", 0), int):
            found.append(("seed-type", "/staff", "staff must be an integer"))
        elif doc.get("staff", 0) >= 1000:
            found.append(("range", "/staff", "staff must be under 1000"))
        return found

    def type_of(self, pointer: str, doc: dict[str, Any]) -> Type | None:
        return self.TYPES.get(pointer)

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        self.writable_calls.append(pointer)
        return pointer not in self.READ_ONLY  # Yes to a reference: it must never be asked

    def is_reference(self, pointer: str, doc: dict[str, Any]) -> bool:
        return pointer in self.REFERENCES


def test_the_toy_model_is_a_model() -> None:
    model: Model = ToyModel()  # mypy checks the protocol
    assert model.dump({"a": 1}) == "a: 1\n"


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    path = tmp_path / "widget.yaml"
    path.write_text(TOY, encoding="utf-8")
    return path


@pytest.fixture
def model() -> ToyModel:
    return ToyModel()


@pytest.fixture
def store(doc: Path, model: ToyModel) -> DocumentStore:
    return DocumentStore(doc, model)


@pytest.fixture
def d(store: DocumentStore, mount: Mount) -> Dispatcher:
    app = Sluice()
    app.route("/widget/{rest*}", virtual=True, handler=Projection(store))
    return mount(app)


def replace(path: Path, text: str) -> None:
    """What the engine does: write a new file beside it and rename it over the old one."""
    new = path.with_suffix(".new")
    new.write_text(text, encoding="utf-8")
    os.replace(new, path)


# Reads: the record's `ls` and `cat` criteria


def test_ls_shows_mappings_as_directories_and_the_rest_as_files(d: Dispatcher) -> None:
    root = run(ls(d, "/widget"))
    assert list(root) == [
        "name", "motto", "summary", "staff", "active", "ratio", "notes", "founded", "updated",
        "tags", "empty", "counts", "address", "contact",
    ]  # fmt: skip
    assert stat.S_ISDIR(root["address"].st_mode)
    assert stat.S_ISDIR(root["contact"].st_mode)
    assert stat.S_ISREG(root["name"].st_mode)
    assert stat.S_ISREG(root["tags"].st_mode)  # A list of scalars is one file
    assert list(run(ls(d, "/widget/address"))) == ["street", "city", "geo"]
    assert list(run(ls(d, "/widget/address/geo"))) == ["lat"]


@pytest.mark.parametrize(
    ("leaf", "content"),
    [
        ("name", b"Example Widget Co\n"),
        ("motto", b"true\n"),  # Declared a string: stays one
        ("summary", b"Makes widgets and sprockets.\n"),  # The value, not its `>-` spelling
        ("staff", b"12\n"),
        ("active", b"true\n"),
        ("ratio", b"0.5\n"),  # No declared type: a float's repr
        ("notes", b""),  # Null: an empty file
        ("founded", b"2001-04-01\n"),
        ("updated", b"2026-11-04T09:30:00+00:00\n"),  # `Z` is spelling; the offset is the value
        ("tags", b"alpha\nbeta\n3\n\n2.5\n"),  # A null item is an empty line
        ("empty", b""),
        ("counts", b"1\n2\n3\n"),
        ("address/street", b"1 High Street\n"),
        ("address/geo/lat", b"51.5\n"),
    ],
)
def test_cat_reads_one_property(d: Dispatcher, leaf: str, content: bytes) -> None:
    assert run(cat(d, f"/widget/{leaf}")) == content


def test_getattr_reports_the_real_size_and_mode_0644_if_writable(d: Dispatcher) -> None:
    for leaf, mode in (
        ("name", 0o444),
        ("notes", 0o644),
        ("tags", 0o644),
        ("contact/vault", 0o444),
    ):
        attr = run(d.getattr(run(walk(d, f"/widget/{leaf}")))).attr
        assert attr.st_size == len(run(cat(d, f"/widget/{leaf}")))
        assert stat.S_IMODE(attr.st_mode) == mode
    root = run(ls(d, "/widget"))
    assert root["staff"].st_size == 3
    assert stat.S_IMODE(root["address"].st_mode) == 0o555


def test_files_open_with_direct_io(d: Dispatcher) -> None:
    info = run(d.open(run(walk(d, "/widget/name")), os.O_RDONLY))
    assert info.direct_io
    run(d.release(info.fh))


def test_misuse(d: Dispatcher) -> None:
    assert errno_of(walk(d, "/widget/nope")) == errno.ENOENT
    assert errno_of(walk(d, "/widget/name/deeper")) == errno.ENOENT
    assert errno_of(d.open(run(walk(d, "/widget/address")), os.O_RDONLY)) == errno.EISDIR
    assert errno_of(d.opendir(run(walk(d, "/widget/name")))) == errno.ENOTDIR


def test_a_value_unlike_its_declared_type_is_eio(doc: Path, mount: Mount) -> None:
    class Wrong(ToyModel):
        TYPES: ClassVar[dict[str, Type]] = {"/staff": "date", "/tags": "string"}

    store = DocumentStore(doc, Wrong())
    app = Sluice()
    app.route("/widget/{rest*}", virtual=True, handler=Projection(store))
    d = mount(app)
    assert errno_of(walk(d, "/widget/staff")) == errno.EIO
    assert errno_of(walk(d, "/widget/tags")) == errno.EIO


def test_pointers_escape_per_rfc_6901() -> None:
    assert pointer(()) == ""
    assert pointer(("a/b", "c~d")) == "/a~1b/c~0d"


# Refused at mount


@pytest.mark.parametrize(
    ("text", "code", "at"),
    [
        ("name: x\nitems:\n  - {a: 1}\n", "list-of-containers", "/items/0"),
        ("name: x\nrun:\n  with:\n    grid: [[1, 2]]\n", "list-of-containers", "/run/with/grid/0"),
        ("name: x\n1: one\n", "key", ""),
        ("name: x\nnested: {a/b: 1}\n", "key", "/nested"),
        ("name: x\nblob: !!binary aGk=\n", "value", "/blob"),
        ("name: x\nitems: [!!binary aGk=]\n", "value", "/items/0"),
        ("- a\n- b\n", "not-a-mapping", ""),
    ],
)
def test_a_shape_the_projection_cannot_show_is_refused_at_mount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str, code: str, at: str
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING), pytest.raises(DocumentError) as e:
        DocumentStore(path, ToyModel())
    assert (e.value.findings[0][0], e.value.findings[0][1]) == (code, at)
    assert code in caplog.text


def test_a_document_with_findings_is_refused_at_mount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: 7\nstaff: many\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING), pytest.raises(DocumentError) as e:
        DocumentStore(path, ToyModel())
    assert e.value.findings == [
        ("required", "/name", "name must be a string"),
        ("seed-type", "/staff", "staff must be an integer"),
    ]
    # Each finding logged at warning: code, pointer, message
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("required at /name: name must be a string" in m for m in warnings)
    assert any("seed-type at /staff: staff must be an integer" in m for m in warnings)
    assert "2 finding(s), first required at /name" in str(e.value)


# Reload at opendir (design 0011, question 2)


def test_a_rename_replace_is_reloaded_at_opendir(doc: Path, d: Dispatcher) -> None:
    before = os.stat(doc).st_ino
    replace(doc, TOY.replace("Example Widget Co", "Renamed Co") + "extra: 1\n")
    assert os.stat(doc).st_ino != before
    assert run(cat(d, "/widget/name")) == b"Example Widget Co\n"  # Reads don't reload
    assert "extra" in run(ls(d, "/widget"))  # opendir does
    assert run(cat(d, "/widget/name")) == b"Renamed Co\n"


def test_an_in_place_edit_is_reloaded_at_opendir(
    doc: Path, store: DocumentStore, d: Dispatcher
) -> None:
    inode = os.stat(doc).st_ino
    doc.write_text(TOY.replace("staff: 12", "staff: 99"), encoding="utf-8")  # Same size
    st = os.stat(doc)
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # Coarse clocks
    assert os.stat(doc).st_ino == inode
    run(ls(d, "/widget"))
    assert run(cat(d, "/widget/staff")) == b"99\n"
    assert store.mtime_ns == os.stat(doc).st_mtime_ns


def test_an_invalid_replacement_keeps_the_last_good_document(
    doc: Path, d: Dispatcher, store: DocumentStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        replace(doc, "name: 7\n")  # Has a finding
        assert "address" in run(ls(d, "/widget"))
        assert "required at /name" in caplog.text
        assert "keeping the last good document" in caplog.text
        caplog.clear()
        run(ls(d, "/widget"))
        assert caplog.text == ""  # Reported once, not at every opendir
        replace(doc, "name: x\nitems: [{a: 1}]\n")  # A shape it can't show
        assert "address" in run(ls(d, "/widget"))
        assert "list-of-containers" in caplog.text
        replace(doc, "name: [unclosed\n")  # Not YAML at all
        assert "address" in run(ls(d, "/widget"))
        assert "doesn't load (ParserError)" in caplog.text
        doc.unlink()
        assert "address" in run(ls(d, "/widget"))
    assert run(cat(d, "/widget/name")) == b"Example Widget Co\n"
    replace(doc, "name: Fixed\n")  # A good one is picked up again
    assert list(run(ls(d, "/widget"))) == ["name"]


# Writes (design 0011 step 4: "A write, step by step")

WRITE = os.O_WRONLY | os.O_TRUNC  # What `echo x > leaf` opens with


async def write(
    d: Dispatcher, path: str, chunks: list[tuple[int, bytes]], flags: int = WRITE
) -> None:
    """Open, write each (offset, data), flush and release: a writer's whole session."""
    info = await d.open(await walk(d, path), flags)
    try:
        for offset, data in chunks:
            assert await d.write(info.fh, offset, data) == len(data)
        await d.flush(info.fh)
    finally:
        await d.release(info.fh)


def echo(d: Dispatcher, path: str, data: bytes) -> None:
    run(write(d, path, [(0, data)]))


def on_disk(doc: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(doc.read_text(encoding="utf-8"))
    return loaded


def only_the_document(doc: Path) -> None:
    """No temporary file is left beside the document."""
    assert sorted(os.listdir(doc.parent)) == [doc.name]


@pytest.mark.parametrize(
    ("leaf", "written", "value"),
    [
        ("motto", b"Widgets for all\n", "Widgets for all"),
        ("motto", b"false\n", "false"),  # Declared a string: the text doesn't make it a boolean
        ("motto", b"two lines\nhere\n", "two lines\nhere"),  # Only one newline is stripped
        ("staff", b"42\n", 42),
        ("staff", b"-7\n", -7),
        ("staff", b"0\n", 0),
        ("active", b"false\n", False),
        ("ratio", b"0.25\n", 0.25),  # No declared type: a float stays a float
        ("founded", b"1999-12-31\n", dt.date(1999, 12, 31)),
        (
            "updated",
            b"2026-11-05T10:00:00+01:00\n",
            dt.datetime(2026, 11, 5, 10, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        ),
        # The whole list is replaced; each item keeps its position's type (design 0012)
        ("tags", b"x\ny z\n4\n\n0.25\n", ["x", "y z", 4, None, 0.25]),
        ("counts", b"", []),
        ("counts", b"1\n2\n3\n4\n", [1, 2, 3, 4]),  # Appended: the last item's type
        ("empty", b"one\n", ["one"]),  # No declared type: a list stays a list, of strings
        ("address/street", b"2 Low Road\n", "2 Low Road"),
        ("address/geo/lat", b"52.25\n", 52.25),
    ],
)
@pytest.mark.usefixtures("no_processes")
def test_read_write_read_round_trips(
    d: Dispatcher, doc: Path, store: DocumentStore, leaf: str, written: bytes, value: Any
) -> None:
    before = run(cat(d, f"/widget/{leaf}"))
    assert before != written
    info = run(d.open(run(walk(d, f"/widget/{leaf}")), WRITE))
    run(d.write(info.fh, 0, written))
    assert run(cat(d, f"/widget/{leaf}")) == before  # Nothing commits before flush
    run(d.flush(info.fh))
    run(d.release(info.fh))
    assert run(cat(d, f"/widget/{leaf}")) == written
    node: Any = on_disk(doc)
    for part in leaf.split("/"):
        node = node[part]
    assert node == value
    assert type(node) is type(value)
    assert on_disk(doc) == store.doc  # The copy was swapped in
    only_the_document(doc)


def test_a_null_leaf_takes_an_empty_write(d: Dispatcher, doc: Path) -> None:
    run(write(d, "/widget/notes", [(0, b"")]))  # A write of 0 bytes, so the handle wrote
    assert on_disk(doc)["notes"] is None
    assert run(cat(d, "/widget/notes")) == b""


def test_key_order_and_the_other_values_are_kept(d: Dispatcher, doc: Path) -> None:
    before = on_disk(doc)
    echo(d, "/widget/staff", b"13\n")
    after = on_disk(doc)
    assert list(after) == list(before)
    assert after == {**before, "staff": 13}


def test_echo_into_a_leaf(d: Dispatcher, doc: Path, model: ToyModel) -> None:
    """`echo 99 > staff`: open with O_TRUNC, one write at 0, flush, release."""
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    assert run(d.read(info.fh, 0, 100)) == b""  # O_TRUNC: the buffer starts empty
    run(d.write(info.fh, 0, b"99\n"))
    run(d.flush(info.fh))
    run(d.flush(info.fh))  # A second flush (a dup'd fd's close) commits nothing more
    run(d.release(info.fh))
    assert on_disk(doc)["staff"] == 99
    assert model.dumps == 1


def test_an_editor_style_write(d: Dispatcher, doc: Path) -> None:
    """Open without O_TRUNC, truncate, several writes, flush."""
    before = doc.read_bytes()
    motto = run(walk(d, "/widget/motto"))
    info = run(d.open(motto, os.O_RDWR))
    assert run(d.read(info.fh, 0, 100)) == b"true\n"  # The current content
    run(d.setattr(motto, SetattrArgs(size=0), fh=info.fh))  # ftruncate: the buffer, not the file
    assert doc.read_bytes() == before
    run(d.write(info.fh, 0, b"Gadgets "))
    run(d.write(info.fh, 8, b"and gizmos"))
    run(d.write(info.fh, 18, b"\n"))
    assert run(d.read(info.fh, 0, 100)) == b"Gadgets and gizmos\n"  # A read sees the buffer
    assert doc.read_bytes() == before
    run(d.flush(info.fh))
    run(d.release(info.fh))
    assert on_disk(doc)["motto"] == "Gadgets and gizmos"


def test_a_write_past_the_end_pads_with_zero_bytes(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    with pytest.raises(FsError) as e:
        run(write(d, "/widget/staff", [(0, b"1"), (3, b"2")]))  # "1\0\02": not an integer
    assert e.value.errno == errno.EINVAL
    assert doc.read_bytes() == before


def test_fsync_commits(d: Dispatcher, doc: Path) -> None:
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    run(d.write(info.fh, 0, b"7\n"))
    run(d.fsync(info.fh, False))
    assert on_disk(doc)["staff"] == 7
    run(d.release(info.fh))


def test_a_handle_that_wrote_nothing_commits_nothing(
    d: Dispatcher, doc: Path, model: ToyModel
) -> None:
    before, mtime = doc.read_bytes(), os.stat(doc).st_mtime_ns
    # Opened without O_TRUNC, closed without a write (with O_TRUNC it commits: design 0012)
    run(write(d, "/widget/staff", [], flags=os.O_WRONLY))
    run(write(d, "/widget/staff", [], flags=os.O_RDWR))
    assert doc.read_bytes() == before
    assert os.stat(doc).st_mtime_ns == mtime
    assert model.dumps == 0


def test_release_only_drops_the_buffer(d: Dispatcher, doc: Path, model: ToyModel) -> None:
    before = doc.read_bytes()
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    run(d.write(info.fh, 0, b"5\n"))
    run(d.release(info.fh))  # No flush
    assert doc.read_bytes() == before
    assert model.dumps == 0
    run(d.setattr(run(walk(d, "/widget/staff")), SetattrArgs(size=1)))  # No buffer left
    assert on_disk(doc)["staff"] == 1


def test_a_read_handle_cannot_write(d: Dispatcher) -> None:
    info = run(d.open(run(walk(d, "/widget/staff")), os.O_RDONLY))
    assert errno_of(d.write(info.fh, 0, b"1\n")) == errno.EBADF
    run(d.flush(info.fh))  # Nothing to commit
    run(d.release(info.fh))


@pytest.mark.parametrize(
    ("leaf", "written"),
    [
        ("staff", b"+1\n"),
        ("staff", b"1_000\n"),
        ("staff", b"012\n"),
        ("staff", b"-0\n"),
        ("staff", b"twelve\n"),
        ("staff", b"12\n\n"),  # Only one newline is stripped
        ("active", b"yes\n"),
        ("active", b"1\n"),
        ("active", b"True\n"),
        ("founded", b"2001-4-1\n"),
        ("founded", b"20010401\n"),
        ("founded", b"2001-02-30\n"),
        ("updated", b"yesterday\n"),
        ("ratio", b"inf\n"),
        ("ratio", b"nan\n"),
        ("ratio", b"half\n"),
        ("motto", b"\xff\xfe\n"),  # Not UTF-8
        ("tags", b"\xff\n"),
        ("notes", b"something\n"),  # A null leaf takes only null
        ("address/geo/lat", b"north\n"),
    ],
)
def test_a_bad_cast_fails_the_write_with_einval(
    d: Dispatcher, doc: Path, store: DocumentStore, leaf: str, written: bytes
) -> None:
    before, held = doc.read_bytes(), store.doc
    info = run(d.open(run(walk(d, f"/widget/{leaf}")), WRITE))
    assert errno_of(d.write(info.fh, 0, written)) == errno.EINVAL
    assert errno_of(d.flush(info.fh)) == errno.EINVAL  # close(2) sees it too
    run(d.release(info.fh))
    assert doc.read_bytes() == before
    assert store.doc is held
    only_the_document(doc)


def test_an_empty_write_is_null_which_no_declared_type_takes(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    for leaf in ("motto", "staff", "active", "founded", "updated", "ratio"):
        info = run(d.open(run(walk(d, f"/widget/{leaf}")), WRITE))
        assert errno_of(d.write(info.fh, 0, b"")) == errno.EINVAL
        run(d.release(info.fh))
    assert doc.read_bytes() == before


def test_a_finding_fails_the_flush_with_einval(
    d: Dispatcher, doc: Path, store: DocumentStore, caplog: pytest.LogCaptureFixture
) -> None:
    before, held = doc.read_bytes(), store.doc
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    run(d.write(info.fh, 0, b"9"))  # Valid as it stands: 9
    run(d.write(info.fh, 1, b"876\n"))  # Not at offset 0: not checked until flush
    with caplog.at_level(logging.WARNING):
        assert errno_of(d.flush(info.fh)) == errno.EINVAL
    run(d.release(info.fh))
    assert doc.read_bytes() == before
    assert store.doc is held
    only_the_document(doc)
    # Each finding at warning, with its code, pointer and message, and never the value
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("range at /staff: staff must be under 1000" in m for m in warnings)
    assert "9876" not in caplog.text


def test_a_finding_at_offset_0_fails_that_write(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    assert errno_of(d.write(info.fh, 0, b"1000\n")) == errno.EINVAL  # What `echo` sees
    run(d.release(info.fh))
    assert doc.read_bytes() == before


def test_a_cast_failure_is_logged_without_the_value(
    d: Dispatcher, caplog: pytest.LogCaptureFixture
) -> None:
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    with caplog.at_level(logging.WARNING):
        assert errno_of(d.write(info.fh, 0, b"sekrit-value\n")) == errno.EINVAL
    run(d.release(info.fh))
    assert "write refused: cast at /staff: isn't a valid integer" in caplog.text
    assert "sekrit" not in caplog.text


def test_a_load_back_mismatch_is_eio(
    d: Dispatcher, doc: Path, store: DocumentStore, model: ToyModel
) -> None:
    before, held = doc.read_bytes(), store.doc
    model.lossy = True  # Its dump drops the motto
    with pytest.raises(FsError) as e:
        echo(d, "/widget/staff", b"13\n")
    assert e.value.errno == errno.EIO
    assert doc.read_bytes() == before
    assert store.doc is held
    only_the_document(doc)


def test_a_dump_or_load_back_that_fails_is_eio(doc: Path, mount: Mount) -> None:
    class Broken(ToyModel):
        def dump(self, doc: dict[str, Any]) -> str:
            raise RuntimeError("cannot dump")

    class Unreadable(ToyModel):
        def __init__(self) -> None:
            super().__init__()
            self.mounted = False

        def load(self, path: Path) -> dict[str, Any]:
            if self.mounted:
                raise ValueError("cannot load")
            return super().load(path)

    before = doc.read_bytes()
    for model in (Broken(), Unreadable()):
        store = DocumentStore(doc, model)
        if isinstance(model, Unreadable):
            model.mounted = True
        app = Sluice()
        app.route("/widget/{rest*}", virtual=True, handler=Projection(store))
        with pytest.raises(FsError) as e:
            echo(mount(app), "/widget/staff", b"13\n")
        assert e.value.errno == errno.EIO
        assert doc.read_bytes() == before
        only_the_document(doc)


def test_an_external_change_is_ebusy_and_kept(
    d: Dispatcher, doc: Path, store: DocumentStore
) -> None:
    info = run(d.open(run(walk(d, "/widget/motto")), WRITE))
    run(d.write(info.fh, 0, b"mine\n"))
    theirs = TOY.replace("staff: 12", "staff: 99")
    doc.write_text(theirs, encoding="utf-8")  # Another writer, in place, the same size
    st = os.stat(doc)
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # Coarse clocks
    assert errno_of(d.flush(info.fh)) == errno.EBUSY
    assert doc.read_text(encoding="utf-8") == theirs  # Left as the other writer left it
    assert store.doc["staff"] == 99  # Reloaded
    run(d.flush(info.fh))  # A retry commits on top of theirs
    run(d.release(info.fh))
    assert on_disk(doc)["staff"] == 99
    assert on_disk(doc)["motto"] == "mine"
    only_the_document(doc)


def test_a_replace_by_another_writer_is_ebusy(d: Dispatcher, doc: Path) -> None:
    info = run(d.open(run(walk(d, "/widget/motto")), WRITE))
    run(d.write(info.fh, 0, b"mine\n"))
    replace(doc, "name: 7\n")  # Theirs doesn't even validate: still theirs to keep
    assert errno_of(d.flush(info.fh)) == errno.EBUSY
    assert errno_of(d.flush(info.fh)) == errno.EBUSY  # Until it is fixed, never overwritten
    run(d.release(info.fh))
    assert doc.read_text(encoding="utf-8") == "name: 7\n"
    doc.unlink()  # Gone altogether: also a change
    assert errno_of(write(d, "/widget/motto", [(0, b"mine\n")])) == errno.EBUSY
    assert not doc.exists()


def test_a_change_during_the_commit_is_ebusy(doc: Path, mount: Mount) -> None:
    class Racing(ToyModel):
        def dump(self, doc_: dict[str, Any]) -> str:
            text = super().dump(doc_)
            replace(doc, TOY.replace("staff: 12", "staff: 98"))  # Lands mid-commit
            return text

    store = DocumentStore(doc, Racing())
    app = Sluice()
    app.route("/widget/{rest*}", virtual=True, handler=Projection(store))
    with pytest.raises(FsError) as e:
        echo(mount(app), "/widget/motto", b"mine\n")
    assert e.value.errno == errno.EBUSY
    assert on_disk(doc)["staff"] == 98
    only_the_document(doc)


def test_concurrent_commits_are_serialised_by_the_lock(doc: Path, mount: Mount) -> None:
    class Slow(ToyModel):
        def __init__(self) -> None:
            super().__init__()
            self.active = self.most = 0
            self.guard = threading.Lock()

        def dump(self, doc: dict[str, Any]) -> str:
            with self.guard:
                self.active += 1
                self.most = max(self.most, self.active)
            time.sleep(0.05)  # Long enough for the other commit to start, unlocked
            with self.guard:
                self.active -= 1
            return super().dump(doc)

    model = Slow()
    store = DocumentStore(doc, model)
    app = Sluice()
    app.route("/widget/{rest*}", virtual=True, handler=Projection(store))
    d = mount(app)

    async def both() -> None:
        a = await d.open(await walk(d, "/widget/motto"), WRITE)
        b = await d.open(await walk(d, "/widget/staff"), WRITE)
        await d.write(a.fh, 0, b"first\n")
        await d.write(b.fh, 0, b"2\n")
        await asyncio.gather(d.flush(a.fh), d.flush(b.fh))
        await d.release(a.fh)
        await d.release(b.fh)

    run(both())
    assert model.most == 1  # One at a time
    assert (on_disk(doc)["motto"], on_disk(doc)["staff"]) == ("first", 2)  # Neither lost


def test_truncate_on_a_path_commits(d: Dispatcher, doc: Path) -> None:
    run(d.setattr(run(walk(d, "/widget/staff")), SetattrArgs(size=1)))  # "12\n" to "1"
    assert on_disk(doc)["staff"] == 1
    run(d.setattr(run(walk(d, "/widget/counts")), SetattrArgs(size=0)))
    assert on_disk(doc)["counts"] == []
    entry = run(d.setattr(run(walk(d, "/widget/motto")), SetattrArgs(size=7)))  # Pads with \0
    assert on_disk(doc)["motto"] == "true\n\0\0"
    assert entry.attr.st_size == len(b"true\n\0\0\n")
    before = doc.read_bytes()
    assert errno_of(d.setattr(run(walk(d, "/widget/staff")), SetattrArgs(size=0))) == errno.EINVAL
    assert doc.read_bytes() == before
    only_the_document(doc)


def test_setattr_on_a_writable_leaf(d: Dispatcher, doc: Path, model: ToyModel) -> None:
    before = doc.read_bytes()
    staff = run(walk(d, "/widget/staff"))
    attr = run(d.setattr(staff, SetattrArgs(atime_ns=1, mtime_ns=2))).attr  # `touch`
    assert stat.S_IMODE(attr.st_mode) == 0o644
    assert errno_of(d.setattr(staff, SetattrArgs(mode=0o600))) == errno.EPERM
    assert errno_of(d.setattr(staff, SetattrArgs(uid=0))) == errno.EPERM
    assert errno_of(d.setattr(staff, SetattrArgs(gid=0))) == errno.EPERM
    assert errno_of(d.setattr(staff, SetattrArgs(mode=0o600, size=0))) == errno.EPERM
    assert doc.read_bytes() == before
    assert model.dumps == 0


def test_a_read_only_leaf_is_eacces(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    name = run(walk(d, "/widget/name"))
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC, WRITE):
        assert errno_of(d.open(name, flags)) == errno.EACCES
    assert errno_of(d.setattr(name, SetattrArgs(size=0))) == errno.EACCES
    assert errno_of(d.setattr(name, SetattrArgs(mtime_ns=1))) == errno.EACCES
    assert not run(d.access(name, os.W_OK))
    assert run(d.access(name, os.R_OK))
    assert run(d.access(run(walk(d, "/widget/staff")), os.W_OK))
    assert doc.read_bytes() == before


def test_shape_changes_are_eperm(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    root = run(walk(d, "/widget"))
    address = run(walk(d, "/widget/address"))
    for directory in (root, address):
        assert errno_of(d.create(directory, "new", 0o644, os.O_WRONLY)) == errno.EPERM
        assert errno_of(d.mkdir(directory, "new", 0o755)) == errno.EPERM
        assert errno_of(d.symlink(directory, "link", "../name")) == errno.EPERM
        assert errno_of(d.setattr(directory, SetattrArgs(mode=0o755))) == errno.EPERM
        assert errno_of(d.setattr(directory, SetattrArgs(mtime_ns=1))) == errno.EPERM
        assert not run(d.access(directory, os.W_OK))
    assert errno_of(d.unlink(root, "staff")) == errno.EPERM
    assert errno_of(d.rmdir(root, "address")) == errno.EPERM
    assert errno_of(d.open(address, WRITE)) == errno.EISDIR
    # The dispatcher refuses a virtual rename before any handler sees it
    assert errno_of(d.rename(root, "staff", root, "headcount")) == errno.EXDEV
    assert doc.read_bytes() == before


def test_a_leaf_gone_since_open_is_not_written(d: Dispatcher, doc: Path) -> None:
    info = run(d.open(run(walk(d, "/widget/motto")), WRITE))
    run(d.write(info.fh, 0, b"mine\n"))
    replace(doc, "name: x\nmotto:\n  nested: true\n")  # Now a directory
    assert "motto" in run(ls(d, "/widget"))  # Reloaded at opendir
    assert errno_of(d.flush(info.fh)) == errno.EISDIR
    replace(doc, "name: x\n")
    run(ls(d, "/widget"))
    assert errno_of(d.flush(info.fh)) == errno.ENOENT
    run(d.release(info.fh))
    assert doc.read_text(encoding="utf-8") == "name: x\n"


def test_the_written_file_keeps_its_mode(d: Dispatcher, doc: Path) -> None:
    os.chmod(doc, 0o640)  # Changes no stamp: the commit goes ahead
    echo(d, "/widget/staff", b"3\n")
    assert stat.S_IMODE(os.stat(doc).st_mode) == 0o640


def test_a_failed_write_back_leaves_no_temporary_file(
    d: Dispatcher, doc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = doc.read_bytes()

    def no_space(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises(FsError) as e:
        echo(d, "/widget/staff", b"3\n")
    assert e.value.errno == errno.ENOSPC
    assert doc.read_bytes() == before
    only_the_document(doc)


# Secret references (the condition of design 0011's approval)


def _no_process(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail("the projector started a process")


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any way to start a process fails the test."""
    for name in ("Popen", "run", "call", "check_call", "check_output", "getoutput"):
        monkeypatch.setattr(subprocess, name, _no_process)
    starters = [n for n in dir(os) if n.startswith(("exec", "spawn", "posix_spawn"))]
    for name in [*starters, "system", "popen", "fork", "forkpty"]:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _no_process)


@pytest.mark.usefixtures("no_processes")
def test_a_reference_leaf_reads_as_its_text_and_is_never_resolved(
    d: Dispatcher, model: ToyModel, store: DocumentStore
) -> None:
    assert model.is_reference("/contact/vault", store.doc)
    assert run(cat(d, "/widget/contact/vault")) == b"pass:services/example/contact\n"
    attr = run(ls(d, "/widget/contact"))["vault"]
    assert attr.st_size == len(b"pass:services/example/contact\n")
    assert stat.S_IMODE(attr.st_mode) == 0o444


@pytest.mark.usefixtures("no_processes")
def test_a_reference_leaf_is_refused_for_writing_before_writable_is_asked(
    d: Dispatcher, model: ToyModel, store: DocumentStore, doc: Path
) -> None:
    before = doc.read_bytes()
    vault = run(walk(d, "/widget/contact/vault"))
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC, os.O_WRONLY | os.O_TRUNC):
        assert errno_of(d.open(vault, flags)) == errno.EACCES
    assert errno_of(d.setattr(vault, SetattrArgs(size=0))) == errno.EACCES
    assert errno_of(d.setattr(vault, SetattrArgs(mode=0o644))) == errno.EACCES
    assert errno_of(d.setattr(vault, SetattrArgs(mtime_ns=1))) == errno.EACCES
    assert not run(d.access(vault, os.W_OK))
    # A commit asks again, at the store: a reference is refused there too, before `writable`
    assert errno_of(store.commit(("contact", "vault"), b"pass:other\n")) == errno.EACCES
    assert "/contact/vault" not in model.writable_calls  # ToyModel.writable says yes to it
    assert doc.read_bytes() == before
    assert model.dumps == 0


def test_the_process_guard_works(no_processes: None) -> None:
    with pytest.raises(pytest.fail.Exception):
        subprocess.run(["true"], check=False)  # noqa: S607  # Patched: never starts
    with pytest.raises(pytest.fail.Exception):
        os.execvp("true", ["true"])  # noqa: S606, S607  # Patched: never starts


def test_repr(store: DocumentStore, doc: Path) -> None:
    assert repr(Projection(store)) == f"Projection({str(doc)!r})"


def test_a_directory_it_cannot_write_in_fails_the_commit(
    d: Dispatcher, doc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = doc.read_bytes()

    def denied(*args: object, **kwargs: object) -> NoReturn:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    with pytest.raises(FsError) as e:
        echo(d, "/widget/staff", b"3\n")
    assert e.value.errno == errno.EACCES
    assert doc.read_bytes() == before


def test_the_written_file_keeps_its_owner_where_it_can(
    d: Dispatcher, doc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    st = os.stat(doc)
    monkeypatch.setattr(os, "geteuid", lambda: st.st_uid + 1)  # As if the mount ran as another
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    echo(d, "/widget/staff", b"4\n")
    assert calls == [(st.st_uid, st.st_gid)]
    assert on_disk(doc)["staff"] == 4


# Design 0012: the write edge cases


def lists(tmp_path: Path, mount: Mount, text: str) -> tuple[Dispatcher, Path]:
    """The projection of a small document of its own, beside the toy one."""
    path = tmp_path / "lists.yaml"
    path.write_text("name: Lists\n" + text, encoding="utf-8")
    app = Sluice()
    app.route("/widget/{rest*}", virtual=True, handler=Projection(DocumentStore(path, ToyModel())))
    return mount(app), path


def test_a_truncating_open_with_no_write_commits(
    d: Dispatcher, doc: Path, model: ToyModel, caplog: pytest.LogCaptureFixture
) -> None:
    """`: > leaf`: one open with O_TRUNC, no write, flush (case 1)."""
    run(write(d, "/widget/counts", []))
    assert on_disk(doc)["counts"] == []  # A list is emptied
    assert run(cat(d, "/widget/counts")) == b""
    before = doc.read_bytes()
    with caplog.at_level(logging.WARNING):
        for leaf in ("staff", "motto", "active", "founded", "updated", "ratio"):
            # Any access mode: Linux truncates on O_RDONLY | O_TRUNC too
            for flags in (WRITE, os.O_RDWR | os.O_TRUNC, os.O_RDONLY | os.O_TRUNC):
                assert errno_of(write(d, f"/widget/{leaf}", [], flags=flags)) == errno.EINVAL
    assert doc.read_bytes() == before  # Byte-identical
    assert "write refused: cast at /staff: an empty file is null" in caplog.text
    only_the_document(doc)
    dumps = model.dumps
    run(write(d, "/widget/notes", []))  # Null takes null: the value it has, so no rewrite
    assert doc.read_bytes() == before
    assert model.dumps == dumps


def test_a_refused_write_keeps_its_bytes_so_flush_never_commits_empty(
    d: Dispatcher, doc: Path
) -> None:
    """`echo bad > counts`: the O_TRUNC open left an empty buffer, which must not commit."""
    before = doc.read_bytes()
    info = run(d.open(run(walk(d, "/widget/counts")), WRITE))
    assert errno_of(d.write(info.fh, 0, b"many\n")) == errno.EINVAL
    assert errno_of(d.flush(info.fh)) == errno.EINVAL
    run(d.release(info.fh))
    assert doc.read_bytes() == before
    assert on_disk(doc)["counts"] == [1, 2, 3]


def test_a_mixed_list_written_back_unchanged_keeps_its_types(
    d: Dispatcher, doc: Path, store: DocumentStore
) -> None:
    """`cat tags > t; cat t > tags` (case 2)."""
    content = run(cat(d, "/widget/tags"))
    echo(d, "/widget/tags", content)
    tags = on_disk(doc)["tags"]
    assert tags == ["alpha", "beta", 3, None, 2.5]
    assert [type(t) for t in tags] == [str, str, int, type(None), float]
    assert store.doc["tags"] == tags
    echo(d, "/widget/tags", b"alpha\nbeta\n4\nnew\n2.5\n")  # Edited in place
    tags = on_disk(doc)["tags"]
    assert tags == ["alpha", "beta", 4, "new", 2.5]  # A null slot in a mixed list: a string
    assert [type(t) for t in tags] == [str, str, int, str, float]


@pytest.mark.parametrize(
    ("current", "written", "value"),
    [
        ("[1, 2]", b"1\n2\n3\n", [1, 2, 3]),  # Past the end: the last non-null item's type
        ("[1, null]", b"1\n\n", [1, None]),  # A null slot: an empty line stays null
        ("[1, null]", b"1\n5\n", [1, 5]),  # Other text: the type the items share
        ("[1, null]", b"1\n\n7\n", [1, None, 7]),  # Past the end of a null: the last non-null
        ("[null, null]", b"\n\nx\n", [None, None, "x"]),  # Only nulls: strings
        ("[a, b]", b"a\n\n", ["a", ""]),  # An empty line in a string slot: the empty string
        ("[a, 3]", b"b\n4\n", ["b", 4]),  # Mixed, same length: by position
        ("[true]", b"false\ntrue\n", [False, True]),
        ("[2026-11-04]", b"2026-11-05\n", [dt.date(2026, 11, 5)]),
        ("[0.5]", b"0.5\n1.25\n", [0.5, 1.25]),
        ("[]", b"3\n", ["3"]),  # An empty list: strings, as in design 0011
    ],
)
def test_written_items_take_their_positions_types(
    tmp_path: Path, mount: Mount, current: str, written: bytes, value: list[Any]
) -> None:
    d, path = lists(tmp_path, mount, f"items: {current}\n")
    echo(d, "/widget/items", written)
    items = on_disk(path)["items"]
    assert items == value
    assert [type(i) for i in items] == [type(v) for v in value]


@pytest.mark.parametrize(
    ("current", "written"),
    [
        ("[1, 2]", b"1\nthree\n"),  # Doesn't cast to its position's type
        ("[1, 2]", b"1\n2\nthree\n"),  # Nor past the end
        ("[1, 2]", b"1\n\n"),  # An empty line in an integer slot
        ("[1, 2]", b"1\n2\n\n"),
        ("[1, null]", b"1\nx\n"),  # A null slot takes the shared type
        ("[true]", b"yes\n"),
    ],
)
def test_an_item_that_does_not_cast_is_einval(
    tmp_path: Path, mount: Mount, current: str, written: bytes
) -> None:
    d, path = lists(tmp_path, mount, f"items: {current}\n")
    before = path.read_bytes()
    info = run(d.open(run(walk(d, "/widget/items")), WRITE))
    assert errno_of(d.write(info.fh, 0, written)) == errno.EINVAL
    run(d.release(info.fh))
    assert path.read_bytes() == before


def test_a_mixed_list_cannot_change_its_length(
    d: Dispatcher, doc: Path, caplog: pytest.LogCaptureFixture
) -> None:
    before = doc.read_bytes()
    with caplog.at_level(logging.WARNING):
        for written in (b"alpha\nbeta\n3\n\n", b"alpha\nbeta\n3\n\n2.5\nsecret-item\n", b""):
            info = run(d.open(run(walk(d, "/widget/tags")), WRITE))
            assert errno_of(d.write(info.fh, 0, written)) == errno.EINVAL
            run(d.release(info.fh))
        assert errno_of(write(d, "/widget/tags", [])) == errno.EINVAL  # `: > tags`
        tags = run(walk(d, "/widget/tags"))
        assert errno_of(d.setattr(tags, SetattrArgs(size=0))) == errno.EINVAL
    assert doc.read_bytes() == before
    assert "write refused: list-types at /tags" in caplog.text
    assert "secret-item" not in caplog.text


def test_a_declared_item_type_wins_over_position() -> None:
    assert cast(b"3\n4\n", "list", ["a", "b"], item="integer") == [3, 4]
    assert cast(b"3\n", "list", [1], item="string") == ["3"]
    assert cast(b"3\n\n", "list", ["a", "b", "c"], item="string") == ["3", ""]  # Any length


def test_a_large_value_is_checked_at_close_only(tmp_path: Path, mount: Mount) -> None:
    """A chunk of `MAX_WRITE_FLOOR` bytes or more may be one piece of a split write (case 3)."""
    d, path = lists(tmp_path, mount, "days: [2026-11-04]\n")
    value = b"2026-11-04\n" * 400  # 4400 bytes: the first chunk ends partway through a date
    chunks = [(0, value[:MAX_WRITE_FLOOR]), (MAX_WRITE_FLOOR, value[MAX_WRITE_FLOOR:])]
    run(write(d, "/widget/days", chunks))
    assert on_disk(path)["days"] == [dt.date(2026, 11, 4)] * 400
    before = path.read_bytes()
    bad = [(0, b"x" * MAX_WRITE_FLOOR)]  # Not a date, but too long to check at write
    assert errno_of(write(d, "/widget/days", bad)) == errno.EINVAL  # Refused at flush
    assert path.read_bytes() == before


def test_a_value_past_the_digit_limit_is_refused_at_close(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    assert run(d.write(info.fh, 0, b"1" * 5000 + b"\n")) == 5001  # Long: not checked yet
    assert errno_of(d.flush(info.fh)) == errno.EINVAL
    run(d.release(info.fh))
    assert doc.read_bytes() == before


def test_a_character_split_across_chunks_commits(d: Dispatcher, doc: Path) -> None:
    """A program writing in fixed-size blocks can split a character between two (case 3)."""
    run(write(d, "/widget/motto", [(0, b"Caf\xc3"), (4, b"\xa9\n")]))
    assert on_disk(doc)["motto"] == "Café"
    run(write(d, "/widget/motto", [(0, b"\xe2\x82"), (2, b"\xac\n")]))  # Two of three bytes
    assert on_disk(doc)["motto"] == "€"
    before = doc.read_bytes()
    assert errno_of(write(d, "/widget/motto", [(0, b"\xc3")])) == errno.EINVAL  # Still split
    assert errno_of(write(d, "/widget/motto", [(0, b"\xff\xc3")])) == errno.EINVAL  # Invalid
    assert doc.read_bytes() == before


def test_one_short_bad_write_still_fails_at_write(d: Dispatcher, doc: Path) -> None:
    before = doc.read_bytes()
    info = run(d.open(run(walk(d, "/widget/staff")), WRITE))
    assert errno_of(d.write(info.fh, 0, b"x" * (MAX_WRITE_FLOOR - 1))) == errno.EINVAL
    run(d.release(info.fh))
    info = run(d.open(run(walk(d, "/widget/motto")), WRITE))
    assert errno_of(d.write(info.fh, 0, b"\xff\n")) == errno.EINVAL  # Invalid, not split
    run(d.release(info.fh))
    assert doc.read_bytes() == before


def test_a_write_of_the_current_value_leaves_the_file_alone(
    d: Dispatcher, doc: Path, model: ToyModel
) -> None:
    """Design 0012, question 1: no dump, no rename, so comments and mtime survive."""
    doc.write_text("# A comment the dump would drop\n" + TOY, encoding="utf-8")
    st = os.stat(doc)
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # Coarse clocks
    run(ls(d, "/widget"))  # Reloaded
    before, mtime = doc.read_bytes(), os.stat(doc).st_mtime_ns
    for leaf in ("staff", "motto", "tags", "updated", "notes", "address/geo/lat", "empty"):
        echo(d, f"/widget/{leaf}", run(cat(d, f"/widget/{leaf}")))
    run(d.setattr(run(walk(d, "/widget/staff")), SetattrArgs(size=3)))  # The size it has
    assert doc.read_bytes() == before
    assert os.stat(doc).st_mtime_ns == mtime
    assert model.dumps == 0
    # Equal to Python, but not the same value: each is written
    echo(d, "/widget/updated", b"2026-11-04T10:30:00+01:00\n")  # The same instant
    assert on_disk(doc)["updated"].utcoffset() == dt.timedelta(hours=1)
    assert model.dumps == 1


def test_the_skip_compares_with_the_document_as_reloaded(
    d: Dispatcher, doc: Path, store: DocumentStore
) -> None:
    """The on-disk check runs first; then the value is compared with the reloaded one."""
    info = run(d.open(run(walk(d, "/widget/motto")), WRITE))
    run(d.write(info.fh, 0, b"true\n"))  # The value it has now
    doc.write_text(TOY.replace("motto: 'true'", "motto: 'okay'"), encoding="utf-8")  # Same size
    st = os.stat(doc)
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # Coarse clocks
    assert errno_of(d.flush(info.fh)) == errno.EBUSY  # Not skipped: the file changed
    assert store.doc["motto"] == "okay"  # Reloaded
    run(d.flush(info.fh))  # Not the reloaded value, so written
    run(d.release(info.fh))
    assert on_disk(doc)["motto"] == "true"
