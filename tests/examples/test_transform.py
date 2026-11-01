import asyncio
import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from transform import Transform

from sluice import Sluice
from sluice.context import SetattrArgs
from sluice.dispatch import Dispatcher

from .conftest import cat, errno_of, ls, run, walk

type Mount = Callable[[Sluice], Dispatcher]


class Counted:
    """`fn`, counting its calls."""

    def __init__(self, fn: Callable[[bytes], bytes]) -> None:
        self.fn = fn
        self.calls = 0

    def __call__(self, data: bytes) -> bytes:
        self.calls += 1
        return self.fn(data)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "sub.txt").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"hello")
    (root / "b.bin").write_bytes(b"raw")
    (root / "big.txt").write_bytes(b"x" * 100)
    return root


def app_with(docs: Path, *transforms: Transform) -> Sluice:
    app = Sluice()
    app.route("/d", backend=str(docs), middlewares=list(transforms))
    return app


def test_reads_sizes_and_listings_are_transformed(docs: Path, mount: Mount) -> None:
    shout = Counted(lambda b: b.upper() + b"!")
    d = mount(app_with(docs, Transform(shout, match="*.txt", max_bytes=50)))
    assert run(cat(d, "/d/a.txt")) == b"HELLO!"
    assert run(cat(d, "/d/b.bin")) == b"raw"
    entries = run(ls(d, "/d"))
    assert entries["a.txt"].st_size == 6
    assert entries["b.bin"].st_size == 3
    assert run(d.getattr(run(walk(d, "/d/a.txt")))).attr.st_size == 6
    assert shout.calls == 1  # Cached by (path, mtime, size) after the first run


def test_a_changed_file_is_transformed_again(docs: Path, mount: Mount) -> None:
    d = mount(app_with(docs, Transform(bytes.upper, match="*.txt")))
    assert run(cat(d, "/d/a.txt")) == b"HELLO"
    (docs / "a.txt").write_bytes(b"bye")
    os.utime(docs / "a.txt", ns=(1, 1))
    assert run(cat(d, "/d/a.txt")) == b"BYE"


def test_a_chain_runs_the_last_transform_first(docs: Path, mount: Mount) -> None:
    def bang(data: bytes) -> bytes:
        return data + b"!"

    d = mount(app_with(docs, Transform(bytes.upper), Transform(bang)))
    assert run(cat(d, "/d/a.txt")) == b"HELLO!"
    assert run(ls(d, "/d"))["a.txt"].st_size == 6
    assert run(d.getattr(run(walk(d, "/d/a.txt")))).attr.st_size == 6


def test_files_over_max_bytes_are_efbig_with_their_raw_size(docs: Path, mount: Mount) -> None:
    d = mount(app_with(docs, Transform(bytes.upper, match="*.txt", max_bytes=50)))
    assert run(ls(d, "/d"))["big.txt"].st_size == 100
    assert errno_of(d.open(run(walk(d, "/d/big.txt")), os.O_RDONLY)) == errno.EFBIG


def test_matching_files_are_read_only(docs: Path, mount: Mount) -> None:
    d = mount(app_with(docs, Transform(bytes.upper, match="*.txt")))
    inode = run(walk(d, "/d/a.txt"))
    assert errno_of(d.open(inode, os.O_WRONLY)) == errno.EROFS
    assert errno_of(d.open(inode, os.O_RDONLY | os.O_TRUNC)) == errno.EROFS
    assert errno_of(d.setattr(inode, SetattrArgs(size=0))) == errno.EROFS
    assert (docs / "a.txt").read_bytes() == b"hello"
    other = run(d.open(run(walk(d, "/d/b.bin")), os.O_WRONLY))
    run(d.release(other.fh))


def test_directories_and_child_routes_pass_through(docs: Path, mount: Mount) -> None:
    (docs / "store").mkdir()
    app = app_with(docs)
    route = app.route("/e", backend=str(docs), middlewares=[Transform(bytes.upper)])
    route.route("child.txt", backend=str(docs / "store"))
    d = mount(app)
    entries = run(ls(d, "/e"))
    assert "child.txt" in entries
    assert entries["sub.txt"].st_mode == (docs / "sub.txt").lstat().st_mode


def test_the_cache_is_bounded_by_bytes(docs: Path, mount: Mount) -> None:
    shout = Counted(bytes.upper)
    d = mount(app_with(docs, Transform(shout, match="*.txt", cache_bytes=100)))
    run(cat(d, "/d/a.txt"))  # 5 bytes cached
    run(cat(d, "/d/big.txt"))  # 100 more: a.txt is evicted
    run(cat(d, "/d/a.txt"))
    assert shout.calls == 3
    assert repr(Transform(bytes.upper)) == "Transform(bytes.upper, match='*')"


def test_concurrent_misses_keep_the_cache_count_true(docs: Path, mount: Mount) -> None:
    t = Transform(bytes.upper, match="*.txt", cache_bytes=10)
    d = mount(app_with(docs, t))

    async def both() -> None:
        await asyncio.gather(cat(d, "/d/a.txt"), cat(d, "/d/a.txt"))

    run(both())
    assert t._cached == sum(len(v) for v in t._cache.values()) == 5


def test_a_chain_keys_and_limits_on_the_source(docs: Path, mount: Mount) -> None:
    def double(data: bytes) -> bytes:
        return data * 2

    outer = Counted(bytes.upper)
    # The inner output (10 bytes) is over the outer limit: the whole chain refuses.
    d = mount(app_with(docs, Transform(outer, max_bytes=8), Transform(double, max_bytes=50)))
    assert run(ls(d, "/d"))["a.txt"].st_size == 5
    assert errno_of(d.open(run(walk(d, "/d/a.txt")), os.O_RDONLY)) == errno.EFBIG
    # The source is over the inner limit but not the outer: refused too, not half-transformed.
    d = mount(app_with(docs, Transform(outer, max_bytes=500), Transform(double, max_bytes=50)))
    assert run(ls(d, "/d"))["big.txt"].st_size == 100
    assert errno_of(d.open(run(walk(d, "/d/big.txt")), os.O_RDONLY)) == errno.EFBIG
    outer.calls = 0
    assert run(cat(d, "/d/a.txt")) == b"HELLOHELLO"
    run(d.getattr(run(walk(d, "/d/a.txt"))))
    run(cat(d, "/d/a.txt"))
    assert outer.calls == 0  # The listing filled the cache: open and getattr use the same key
