import asyncio
import errno
import os
import posixpath
import stat
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from layers import Layer, by_date, by_tag, by_type, scan

from sluice import Sluice
from sluice.dispatch import Dispatcher

from .conftest import cat, errno_of, ls, run, walk

type Mount = Callable[[Sluice], Dispatcher]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "project.py").write_text("print()", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    (root / "docs" / "project.py").write_text("# docs copy", encoding="utf-8")
    (root / "Makefile").write_text("all:", encoding="utf-8")
    (root / "link.py").symlink_to("project.py")
    day = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))
    os.utime(root / "docs" / "guide.md", (day, day))
    return root


@pytest.fixture
def d(project: Path, mount: Mount) -> Dispatcher:
    app = Sluice()
    app.route("/src", backend=str(project))
    app.route("/by-type/{rest*}", virtual=True, handler=Layer(project, "/src", by_type))
    app.route("/by-date/{rest*}", virtual=True, handler=Layer(project, "/src", by_date))
    return mount(app)


def resolve(d: Dispatcher, path: str) -> str:
    """Where the kernel would go for the link at `path`."""
    link = run(d.readlink(run(walk(d, path))))
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), link))


def test_views_list_directories_by_class(d: Dispatcher) -> None:
    root = run(ls(d, "/by-type"))
    assert set(root) == {"md", "none", "py"}
    assert all(stat.S_ISDIR(a.st_mode) for a in root.values())
    assert set(run(ls(d, "/by-type/py"))) == {"project@docs.py", "project@.py"}
    assert set(run(ls(d, "/by-date/2026/10/31"))) == {"guide.md"}


def test_entries_are_relative_links_into_the_backed_route(d: Dispatcher) -> None:
    link = run(d.readlink(run(walk(d, "/by-type/py/project@docs.py"))))
    assert link == "../../src/docs/project.py"
    assert resolve(d, "/by-type/py/project@.py") == "/src/project.py"
    assert resolve(d, "/by-date/2026/10/31/guide.md") == "/src/docs/guide.md"
    assert run(cat(d, resolve(d, "/by-type/md/guide.md"))) == b"# Guide"


def test_link_attributes_match_readlink(d: Dispatcher) -> None:
    entries = run(ls(d, "/by-type/md"))
    attr = entries["guide.md"]
    assert stat.S_ISLNK(attr.st_mode)
    assert attr.st_size == len("../../src/docs/guide.md")
    looked_up = run(d.getattr(run(walk(d, "/by-type/md/guide.md")))).attr
    assert looked_up.st_size == attr.st_size


def test_unknown_names_and_misuse(d: Dispatcher) -> None:
    assert errno_of(walk(d, "/by-type/rs")) == errno.ENOENT
    assert errno_of(walk(d, "/by-type/py/nope.py")) == errno.ENOENT
    py = run(walk(d, "/by-type/py"))
    assert errno_of(d.readlink(py)) == errno.EINVAL
    assert errno_of(d.open(py, os.O_RDONLY)) == errno.EISDIR
    assert errno_of(d.opendir(run(walk(d, "/by-type/py/project@.py")))) == errno.ENOTDIR
    assert errno_of(d.mkdir(py, "x", 0o755)) == errno.EROFS


def test_the_scan_skips_symlinks(project: Path) -> None:
    tree = scan(project, by_type)
    assert "link.py" not in tree[("py",)]


def test_names_keep_their_file_when_files_are_added(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.py").write_text("a", encoding="utf-8")
    (tmp_path / "a" / ".rc").write_text("a", encoding="utf-8")
    (tmp_path / "b" / ".rc").write_text("b", encoding="utf-8")
    before = scan(tmp_path, by_type)
    assert before[("py",)] == {"x.py": PurePosixPath("a/x.py")}
    assert set(before[("none",)]) == {".rc@a", ".rc@b"}  # Still dotfiles
    (tmp_path / "b" / "x.py").write_text("b", encoding="utf-8")
    after = scan(tmp_path, by_type)
    assert after[("py",)] == {"x@a.py": PurePosixPath("a/x.py"), "x@b.py": PurePosixPath("b/x.py")}


def test_a_view_named_twice_lists_the_file_once(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("f", encoding="utf-8")
    assert scan(tmp_path, lambda rel, real, st: ["t", "t/"])[("t",)] == {"f": PurePosixPath("f")}


def test_a_file_and_a_directory_of_one_name_both_stay(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a", encoding="utf-8")
    (tmp_path / "b").write_text("b", encoding="utf-8")

    def odd(rel: PurePosixPath, real: Path, st: os.stat_result) -> list[str]:
        return [""] if rel.name == "a" else ["a"]

    tree = scan(tmp_path, odd)
    assert tree[()] == {"a": None, "a@": PurePosixPath("a")}
    assert tree[("a",)] == {"b": PurePosixPath("b")}


def test_views_cannot_climb_out(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("f", encoding="utf-8")
    tree = scan(tmp_path, lambda rel, real, st: ["../../x/./y"])
    assert ("x", "y") in tree


def test_by_tag_reads_the_user_tags_xattr(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("f", encoding="utf-8")
    st = f.stat()
    assert list(by_tag(PurePosixPath("f"), f, st)) == []
    try:
        os.setxattr(f, "user.tags", b"work, urgent,work")
    except OSError:
        pytest.skip("no user xattrs on this filesystem")
    assert list(by_tag(PurePosixPath("f"), f, st)) == ["work", "urgent"]


def test_a_failed_rebuild_waits_for_the_next_refresh(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    calls: list[str] = []

    def flaky(rel: PurePosixPath, real: Path, st: os.stat_result) -> list[str]:
        calls.append(rel.name)
        if len(calls) > 1:
            raise RuntimeError("classify bug")
        return ["py"]

    layer = Layer(tmp_path, "/src", flaky, refresh=3600)

    async def go() -> None:
        await layer.index()
        layer._built -= 7200  # The index is stale: one rebuild starts, and fails
        await layer.index()
        assert layer._rebuild is not None
        await layer._rebuild
        await layer.index()  # No retry before another refresh interval
        assert layer._rebuild is None

    asyncio.run(go())
    assert calls == ["a.py", "a.py"]


def test_the_index_refreshes_in_the_background(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    layer = Layer(tmp_path, "/src", by_type, refresh=0)

    async def go() -> tuple[set[str], set[str]]:
        first = set((await layer.index())[("py",)])
        (tmp_path / "b.py").write_text("b", encoding="utf-8")
        stale = await layer.index()  # Starts a rebuild, serves the old tree
        assert layer._rebuild is not None
        await layer._rebuild
        return first | set(stale[("py",)]), set((await layer.index())[("py",)])

    before, after = asyncio.run(go())
    assert before == {"a.py"}
    assert after == {"a.py", "b.py"}
    assert repr(layer) == f"Layer({str(tmp_path)!r}, '/src')"
