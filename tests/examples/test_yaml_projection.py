"""The generic YAML projector (design 0011, step 2) on a toy document and a toy pyyaml model."""

import errno
import logging
import os
import stat
import subprocess
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
    pointer,
)

from sluice import Sluice
from sluice.context import SetattrArgs
from sluice.dispatch import Dispatcher

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

    def __init__(self) -> None:
        self.writable_calls: list[str] = []

    def load(self, path: Path) -> dict[str, Any]:
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return doc

    def dump(self, doc: dict[str, Any]) -> str:
        return yaml.safe_dump(doc, sort_keys=False)

    def validate(self, doc: dict[str, Any]) -> list[Finding]:
        found: list[Finding] = []
        if not isinstance(doc.get("name"), str):
            found.append(("required", "/name", "name must be a string"))
        if not isinstance(doc.get("staff", 0), int):
            found.append(("seed-type", "/staff", "staff must be an integer"))
        return found

    def type_of(self, pointer: str, doc: dict[str, Any]) -> Type | None:
        return self.TYPES.get(pointer)

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        self.writable_calls.append(pointer)
        return True  # Says yes everywhere: the read-only step and references must not ask

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
        "tags", "empty", "address", "contact",
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
        ("address/street", b"1 High Street\n"),
        ("address/geo/lat", b"51.5\n"),
    ],
)
def test_cat_reads_one_property(d: Dispatcher, leaf: str, content: bytes) -> None:
    assert run(cat(d, f"/widget/{leaf}")) == content


def test_getattr_reports_the_real_size_and_mode_0444(d: Dispatcher) -> None:
    for leaf in ("name", "notes", "tags", "contact/vault"):
        attr = run(d.getattr(run(walk(d, f"/widget/{leaf}")))).attr
        assert attr.st_size == len(run(cat(d, f"/widget/{leaf}")))
        assert stat.S_IMODE(attr.st_mode) == 0o444
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


# Read-only


def test_every_write_operation_is_refused(d: Dispatcher, model: ToyModel) -> None:
    root = run(walk(d, "/widget"))
    name = run(walk(d, "/widget/name"))
    address = run(walk(d, "/widget/address"))
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC):
        assert errno_of(d.open(name, flags)) == errno.EROFS
    assert errno_of(d.setattr(name, SetattrArgs(size=0))) == errno.EROFS  # truncate(2)
    assert errno_of(d.setattr(name, SetattrArgs(mode=0o644))) == errno.EROFS
    assert errno_of(d.create(root, "new", 0o644, os.O_WRONLY)) == errno.EROFS
    assert errno_of(d.mkdir(root, "new", 0o755)) == errno.EROFS
    assert errno_of(d.unlink(root, "name")) == errno.EROFS
    assert errno_of(d.rmdir(root, "address")) == errno.EROFS
    assert errno_of(d.symlink(address, "link", "../name")) == errno.EROFS
    assert errno_of(d.setxattr(name, "user.x", b"1")) == errno.EROFS
    assert errno_of(d.removexattr(name, "user.x")) == errno.EROFS
    # The dispatcher refuses a virtual rename before any handler sees it; the handler would too
    assert errno_of(d.rename(root, "name", root, "title")) == errno.EXDEV
    assert not run(d.access(name, os.W_OK))
    assert model.writable_calls == []  # Never consulted in the read-only step


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
    d: Dispatcher, model: ToyModel
) -> None:
    vault = run(walk(d, "/widget/contact/vault"))
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC):
        assert errno_of(d.open(vault, flags)) == errno.EACCES
    assert errno_of(d.setattr(vault, SetattrArgs(size=0))) == errno.EACCES
    assert errno_of(d.setattr(vault, SetattrArgs(mode=0o644))) == errno.EACCES
    assert model.writable_calls == []  # ToyModel.writable says yes: it was never asked


def test_the_process_guard_works(no_processes: None) -> None:
    with pytest.raises(pytest.fail.Exception):
        subprocess.run(["true"], check=False)  # noqa: S607  # Patched: never starts
    with pytest.raises(pytest.fail.Exception):
        os.execvp("true", ["true"])  # noqa: S606, S607  # Patched: never starts


def test_repr(store: DocumentStore, doc: Path) -> None:
    assert repr(Projection(store)) == f"Projection({str(doc)!r})"
