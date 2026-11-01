"""`ctx.backend`: the read-only backend view middleware does its I/O through."""

import asyncio
import errno
import os
import stat
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from sluice import BackendView, Context, FsError, Middleware, Sluice, VirtualHandler
from sluice.backend import Passthrough
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE

type Probe = Callable[[BackendView], Coroutine[Any, Any, Any]]


class Capture(Middleware):
    """Records `ctx.backend` from the first call it sees."""

    ops = frozenset({"lookup"})

    def __init__(self) -> None:
        self.seen: list[BackendView | None] = []

    async def pre(self, ctx: Context) -> None:
        self.seen.append(ctx.backend)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "alice"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"hello")
    (root / "sub" / "b.txt").write_bytes(b"b")
    (root / "inside").symlink_to("sub/b.txt")
    (root / "chain").symlink_to("inside")
    (root / "dirlink").symlink_to("sub")
    (root / "loop").symlink_to("loop")
    (tmp_path / "data" / "bob.txt").write_bytes(b"bob's")
    (root / "sideways").symlink_to("../bob.txt")
    (root / "out").symlink_to(tmp_path)
    os.mkfifo(root / "fifo")
    return tmp_path


@pytest.fixture
def capture() -> Capture:
    return Capture()


@pytest.fixture
def dispatcher(tree: Path, capture: Capture) -> Iterator[Dispatcher]:
    app = Sluice()
    app.route("/u/{user}", backend=f"{tree}/data/{{user}}", middlewares=[capture])
    app.route("/v/status", virtual=True, handler=VirtualHandler(), middlewares=[capture])
    io = Passthrough(2)
    yield Dispatcher(app.freeze(), io)
    io.shutdown()


def view(d: Dispatcher, capture: Capture, path: str = "/u/alice") -> BackendView | None:
    async def walk() -> None:
        inode = ROOT_INODE
        for name in path.split("/")[1:]:
            inode = (await d.lookup(inode, name)).inode

    asyncio.run(walk())
    return capture.seen[-1]


def run(d: Dispatcher, capture: Capture, probe: Probe) -> Any:
    backend = view(d, capture)
    assert backend is not None
    return asyncio.run(probe(backend))


def errno_of(d: Dispatcher, capture: Capture, probe: Probe) -> int:
    with pytest.raises(FsError) as e:
        run(d, capture, probe)
    return e.value.errno


def test_root_is_the_rendered_backend(dispatcher: Dispatcher, capture: Capture, tree: Path) -> None:
    backend = view(dispatcher, capture)
    assert backend is not None
    assert backend.root == tree / "data" / "alice"


def test_virtual_routes_have_no_backend(dispatcher: Dispatcher, capture: Capture) -> None:
    assert view(dispatcher, capture, "/v/status") is None


def test_paths_are_absolute_or_relative_to_the_root(
    dispatcher: Dispatcher, capture: Capture, tree: Path
) -> None:
    real = tree / "data" / "alice" / "a.txt"
    assert run(dispatcher, capture, lambda b: b.read_all(real, 100)) == b"hello"
    assert run(dispatcher, capture, lambda b: b.read_all("a.txt", 100)) == b"hello"


@pytest.mark.parametrize("path", ["../bob.txt", "sub/../../bob.txt", "/etc/passwd"])
def test_paths_outside_the_root_are_eacces(
    dispatcher: Dispatcher, capture: Capture, path: str
) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.lstat(path)) == errno.EACCES


def test_lstat_does_not_follow(dispatcher: Dispatcher, capture: Capture) -> None:
    attr = run(dispatcher, capture, lambda b: b.lstat("inside"))
    assert stat.S_ISLNK(attr.st_mode)


def test_stat_follows_links_inside_the_root(dispatcher: Dispatcher, capture: Capture) -> None:
    attr = run(dispatcher, capture, lambda b: b.stat("chain"))
    assert stat.S_ISREG(attr.st_mode)
    assert attr.st_size == 1
    assert stat.S_ISDIR(run(dispatcher, capture, lambda b: b.stat("dirlink")).st_mode)


@pytest.mark.parametrize("name", ["sideways", "out"])
def test_stat_refuses_links_out_of_the_root(
    dispatcher: Dispatcher, capture: Capture, name: str
) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.stat(name)) == errno.EACCES


def test_stat_of_a_link_loop_is_eloop(dispatcher: Dispatcher, capture: Capture) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.stat("loop")) == errno.ELOOP


def test_a_link_in_a_directory_component_is_not_followed(
    dispatcher: Dispatcher, capture: Capture
) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.stat("dirlink/b.txt")) == errno.ENOTDIR
    assert errno_of(dispatcher, capture, lambda b: b.scandir("out")) == errno.ENOTDIR


def test_missing_files_keep_their_errno(dispatcher: Dispatcher, capture: Capture) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.lstat("nope")) == errno.ENOENT
    assert errno_of(dispatcher, capture, lambda b: b.stat("nope")) == errno.ENOENT
    assert errno_of(dispatcher, capture, lambda b: b.read_all("nope", 9)) == errno.ENOENT


def test_read_all_enforces_its_limit(dispatcher: Dispatcher, capture: Capture) -> None:
    assert run(dispatcher, capture, lambda b: b.read_all("a.txt", 5)) == b"hello"
    assert errno_of(dispatcher, capture, lambda b: b.read_all("a.txt", 4)) == errno.EFBIG


@pytest.mark.parametrize(
    ("name", "error"),
    [("inside", errno.ELOOP), ("sub", errno.EISDIR), ("fifo", errno.EINVAL)],
)
def test_read_all_reads_only_regular_files(
    dispatcher: Dispatcher, capture: Capture, name: str, error: int
) -> None:
    assert errno_of(dispatcher, capture, lambda b: b.read_all(name, 100)) == error


def test_scandir_lists_with_lstat_attributes(dispatcher: Dispatcher, capture: Capture) -> None:
    entries = {e.name: e.attr for e in run(dispatcher, capture, lambda b: b.scandir("."))}
    assert {"a.txt", "sub", "inside"} <= entries.keys()
    assert entries["a.txt"].st_size == 5
    assert stat.S_ISLNK(entries["inside"].st_mode)
