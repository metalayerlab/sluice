import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from strm import Strm

from sluice import Sluice
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE

from .conftest import cat, errno_of, ls, run, walk

URL = "http://media.example.org/{rel}"


@pytest.fixture
def media(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    (root / "Films" / "Extras").mkdir(parents=True)
    (root / "Films" / "Heat.mkv").write_bytes(b"\0" * 1000)
    (root / "Films" / "Heat.nfo").write_text("<movie/>", encoding="utf-8")
    (root / "Films" / "Heat 2.MP4").write_bytes(b"\0")
    (root / "Films" / "Ronin.mkv").symlink_to("Heat.mkv")
    (root / "Films" / "Gone.mkv").symlink_to("missing.mkv")
    (tmp_path / "outside.mkv").write_bytes(b"\0")
    (root / "Films" / "Out.mkv").symlink_to(tmp_path / "outside.mkv")
    (root / "Films" / "Folder.mkv").mkdir()
    (root / "Films" / "Real.mkv.strm").mkdir()
    (root / "Films" / "Loop.mkv").symlink_to("Loop.mkv")
    return root


@pytest.fixture
def d(media: Path, mount: Callable[[Sluice], Dispatcher]) -> Dispatcher:
    app = Sluice()
    app.route("/media", backend=str(media), middlewares=[Strm(URL)])
    return mount(app)


def test_lists_videos_as_strm_files_and_hides_the_rest(d: Dispatcher) -> None:
    entries = run(ls(d, "/media/Films"))
    assert set(entries) == {
        "Extras",
        "Folder.mkv",
        "Heat.mkv.strm",
        "Heat 2.MP4.strm",
        "Ronin.mkv.strm",
    }
    body = b"http://media.example.org/Films/Heat.mkv\n"
    assert entries["Heat.mkv.strm"].st_size == len(body) == 40
    assert stat.S_IMODE(entries["Heat.mkv.strm"].st_mode) == 0o444
    assert stat.S_ISDIR(entries["Extras"].st_mode)


def test_reads_the_location(d: Dispatcher) -> None:
    assert run(cat(d, "/media/Films/Heat.mkv.strm")) == b"http://media.example.org/Films/Heat.mkv\n"
    quoted = b"http://media.example.org/Films/Heat%202.MP4\n"
    assert run(cat(d, "/media/Films/Heat 2.MP4.strm")) == quoted


def test_a_symlinked_video_inside_the_backend_counts(d: Dispatcher) -> None:
    body = run(cat(d, "/media/Films/Ronin.mkv.strm"))
    assert body == b"http://media.example.org/Films/Ronin.mkv\n"


def test_lookup_agrees_with_the_listing(d: Dispatcher, media: Path) -> None:
    attr = run(d.getattr(run(walk(d, "/media/Films/Heat.mkv.strm")))).attr
    assert attr.st_size == 40
    assert attr.st_mtime_ns == (media / "Films" / "Heat.mkv").stat().st_mtime_ns
    hidden = ["Heat.mkv", "Heat.nfo", "Heat.nfo.strm", "Nope.mkv.strm", "Gone.mkv.strm"]
    hidden += ["Out.mkv.strm", "Loop.mkv.strm", "Folder.mkv.strm", "Real.mkv.strm"]
    for name in hidden:
        assert errno_of(walk(d, f"/media/Films/{name}")) == errno.ENOENT, name


def test_is_read_only(d: Dispatcher) -> None:
    strm = run(walk(d, "/media/Films/Heat.mkv.strm"))
    films = run(walk(d, "/media/Films"))
    assert errno_of(d.open(strm, os.O_WRONLY)) == errno.EROFS
    assert errno_of(d.open(strm, os.O_RDWR)) == errno.EROFS
    assert errno_of(d.open(strm, os.O_RDONLY | os.O_TRUNC)) == errno.EROFS
    assert errno_of(d.mkdir(films, "new", 0o755)) == errno.EROFS
    assert errno_of(d.unlink(films, "Heat.mkv.strm")) == errno.EROFS
    assert errno_of(d.create(films, "x.mkv.strm", 0o644, os.O_WRONLY)) == errno.EROFS
    assert errno_of(d.rename(films, "Heat.mkv.strm", films, "y.mkv.strm")) == errno.EROFS


def test_other_path_ops_on_a_strm_file(d: Dispatcher) -> None:
    strm = run(walk(d, "/media/Films/Heat.mkv.strm"))
    assert run(d.access(strm, os.R_OK)) is True
    assert run(d.access(strm, os.W_OK)) is False
    assert run(d.access(strm, os.X_OK)) is False
    assert run(d.access(run(walk(d, "/media/Films")), os.W_OK)) is False
    assert run(d.listxattr(strm)) == []
    assert errno_of(d.getxattr(strm, "user.x")) == errno.ENODATA
    assert errno_of(d.readlink(strm)) == errno.EINVAL
    assert errno_of(d.opendir(strm)) == errno.ENOTDIR


def test_the_route_root_lists(d: Dispatcher) -> None:
    root = run(d.lookup(ROOT_INODE, "media"))
    assert stat.S_ISDIR(root.attr.st_mode)
    assert set(run(ls(d, "/media"))) == {"Films"}


def test_name_and_video_suffixes_are_configurable(
    media: Path, mount: Callable[[Sluice], Dispatcher]
) -> None:
    app = Sluice()
    app.route("/m", backend=str(media), middlewares=[Strm("/srv/{name}", video={".NFO"})])
    d = mount(app)
    assert set(run(ls(d, "/m/Films"))) == {"Extras", "Folder.mkv", "Heat.nfo.strm"}
    assert run(cat(d, "/m/Films/Heat.nfo.strm")) == b"/srv/Heat.nfo\n"
    assert repr(Strm(URL)) == f"Strm({URL!r})"


@pytest.mark.parametrize("template", ["http://x/{path}", "{0}", "{rel"])
def test_a_bad_template_fails_at_setup(template: str) -> None:
    with pytest.raises(ValueError, match="Strm template"):
        Strm(template)
