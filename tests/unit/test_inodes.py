import asyncio
import errno
from pathlib import PurePosixPath as P

import pytest

from sluice import Context, FsError, MountConfig, Sluice, VirtualHandler
from sluice.backend import Passthrough
from sluice.context import RenameArgs
from sluice.inodes import ROOT_INODE, InodeTable


def test_dot_and_dotdot() -> None:
    t = InodeTable()
    a = t.lookup(P("/a"))
    assert t.child(a, ".") == P("/a")
    assert t.child(a, "..") == P("/")
    assert t.child(ROOT_INODE, "b") == P("/b")


def test_lookup_counts_and_forget() -> None:
    t = InodeTable()
    a = t.lookup(P("/a"))
    assert t.lookup(P("/a")) == a
    t.forget(a, 1)
    assert t.path(a) == P("/a")  # one lookup left
    t.forget(a, 1)
    with pytest.raises(FsError):
        t.path(a)
    t.forget(ROOT_INODE, 5)
    assert t.path(ROOT_INODE) == P("/")


def test_rename_moves_descendants_and_detaches_target() -> None:
    t = InodeTable()
    d = t.lookup(P("/d"))
    f = t.lookup(P("/d/f"))
    old_target = t.lookup(P("/e"))
    t.rename(P("/d"), P("/e"))
    assert t.path(d) == P("/e")
    assert t.path(f) == P("/e/f")
    assert t.detached(old_target)  # lives on, for its open files, until forgotten
    with pytest.raises(FsError):
        t.path(old_target)  # but its old path names the moved file now
    before = len(t)
    t.forget(old_target, 1)
    assert len(t) == before - 1
    assert not t.detached(old_target)
    assert t.path(d) == P("/e")
    assert t.find(P("/d")) == 0


def test_rename_exchange_swaps() -> None:
    t = InodeTable()
    a, b = t.lookup(P("/a")), t.lookup(P("/b"))
    t.rename(P("/a"), P("/b"), exchange=True)
    assert t.path(a) == P("/b")
    assert t.path(b) == P("/a")


def test_forget_after_unlink_and_reuse_of_name() -> None:
    t = InodeTable()
    a = t.lookup(P("/a"))
    t.unlink(P("/a"))
    assert t.detached(a)
    b = t.lookup(P("/a"))
    assert a != b
    assert not t.detached(b)
    t.forget(a, 1)
    assert t.path(b) == P("/a")
    assert len(t) == 2


def test_detaching_an_unheld_inode_drops_it() -> None:
    t = InodeTable()
    listed = t.ensure(P("/d/f"))  # from readdir: no lookup, so no forget will come
    held = t.lookup(P("/d"))
    t.unlink(P("/d"))
    assert t.detached(held)
    assert not t.detached(listed)
    assert len(t) == 2  # the root and the held inode


def test_passthrough_needs_a_real_path() -> None:
    app = Sluice()
    app.route("/v", virtual=True, handler=VirtualHandler())
    res = app.freeze().resolve("/v")
    assert res is not None
    ctx = Context("rename", res, config=MountConfig(), args=RenameArgs(P("/w")))
    io = Passthrough(1)

    async def go(name: str) -> None:
        await getattr(io, name)(ctx)

    for name in ("getattr", "rename"):
        with pytest.raises(FsError) as e:
            asyncio.run(go(name))
        assert e.value.errno in {errno.EIO, errno.EXDEV}
    io.shutdown()
