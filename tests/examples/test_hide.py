import errno
import os
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from hide import Hide, glob, larger_than, older_than

from sluice import Attr, Sluice
from sluice.dispatch import Dispatcher

from .conftest import cat, errno_of, ls, run, walk

type Mount = Callable[[Sluice], Dispatcher]


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    (root / "build" / "obj").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "main.c").write_text("int main;", encoding="utf-8")
    (root / "src" / "main.o.tmp").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / "big.iso").write_bytes(b"\0" * 2000)
    (root / "build" / "obj" / "a.o").write_bytes(b"o")
    old = root / "old.log"
    old.write_text("old", encoding="utf-8")
    week = time.time() - 7 * 86400
    os.utime(old, (week, week))
    return root


@pytest.fixture
def d(share: Path, mount: Mount) -> Dispatcher:
    app = Sluice()
    rules = Hide(glob(".*"), glob("*.tmp"), glob("build/**"), larger_than(1000), older_than(86400))
    app.route("/s", backend=str(share), middlewares=[rules])
    return mount(app)


def test_listings_drop_hidden_entries(d: Dispatcher) -> None:
    assert set(run(ls(d, "/s"))) == {"build", "src"}
    assert set(run(ls(d, "/s/src"))) == {"main.c"}
    assert set(run(ls(d, "/s/build"))) == set()


@pytest.mark.parametrize("path", ["/s/.git", "/s/src/main.o.tmp", "/s/big.iso", "/s/old.log"])
def test_lookups_agree_with_listings(d: Dispatcher, path: str) -> None:
    assert errno_of(walk(d, path)) == errno.ENOENT


def test_child_routes_are_listed_and_reachable(share: Path, mount: Mount) -> None:
    (share / "cache-store").mkdir()
    app = Sluice()
    route = app.route("/s", backend=str(share), middlewares=[Hide(glob(".*"))])
    route.route(".cache", backend=str(share / "cache-store"))
    d = mount(app)
    assert ".cache" in run(ls(d, "/s"))
    assert run(walk(d, "/s/.cache")) > 0
    assert ".git" not in run(ls(d, "/s"))


def test_a_double_star_glob_hides_a_whole_subtree(d: Dispatcher) -> None:
    assert errno_of(walk(d, "/s/build/obj")) == errno.ENOENT
    assert run(walk(d, "/s/build")) > 0


def test_visible_files_work(d: Dispatcher, share: Path) -> None:
    assert run(cat(d, "/s/src/main.c")) == b"int main;"
    src = run(walk(d, "/s/src"))
    info, _ = run(d.create(src, "new.c", 0o644, os.O_WRONLY))
    run(d.release(info.fh))
    assert (share / "src" / "new.c").exists()


def test_making_a_hidden_name_is_eacces(d: Dispatcher, share: Path) -> None:
    src = run(walk(d, "/s/src"))
    assert errno_of(d.create(src, "x.tmp", 0o644, os.O_WRONLY)) == errno.EACCES
    assert errno_of(d.mkdir(src, ".cache", 0o755)) == errno.EACCES
    assert errno_of(d.rename(src, "main.c", src, "main.c.tmp")) == errno.EACCES
    assert (share / "src" / "main.c").exists()


def test_renaming_a_hidden_file_out_is_enoent(d: Dispatcher, share: Path) -> None:
    src = run(walk(d, "/s/src"))
    assert errno_of(d.rename(src, "main.o.tmp", src, "visible.o")) == errno.ENOENT
    assert (share / "src" / "main.o.tmp").exists()


def test_size_and_age_rules_ignore_directories_and_new_names() -> None:
    directory = Attr.directory()
    rel = PurePosixPath("d")
    assert not larger_than(0)(rel, directory)
    assert not older_than(0, clock=lambda: 1e12)(rel, directory)
    assert not larger_than(0)(rel, None)
    assert not older_than(0)(rel, None)


def test_globs() -> None:
    def hit(pattern: str, path: str) -> bool:
        return glob(pattern)(PurePosixPath(path), None)

    assert hit("*.tmp", "a/b/c.tmp")
    assert not hit("*.tmp", "a.tmp/c")
    assert hit("src/*.c", "src/a.c")
    assert not hit("src/*.c", "src/sub/a.c")
    assert hit("src/**/*.c", "src/sub/deep/a.c")
    assert hit("src/**/*.c", "src/a.c")
    assert hit("/src/?.c", "src/a.c")
    assert not hit("src/?.c", "src/ab.c")
    assert hit("src/*.[oa]", "src/x.o")
    assert not hit("src/*.[oa]", "src/x.c")
    assert hit("src/*.[!c]", "src/x.h")
    assert not hit("src/*.[!c]", "src/x.c")
    assert hit("src/[a-c].h", "src/b.h")
    assert repr(Hide(glob("*"))) == "Hide(1 rule)"
