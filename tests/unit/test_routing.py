from collections.abc import Callable
from pathlib import Path

import pytest

from sluice import Context, FsError, MountConfig, RouteError, Sluice, VirtualHandler
from sluice.routing import Kind, parse_pattern


class Status(VirtualHandler):
    pass


async def ok(ctx: Context) -> None:
    return None


async def decode(ctx: Context, raw: object) -> object:
    return raw


def tenant_app() -> Sluice:
    """The design's example tree."""
    app = Sluice()
    user = app.route("/fuse/{user}/{token}", backend="/ext4/{user}", middlewares=[ok])
    user.route("private", middlewares=[ok])
    user.route("status", virtual=True, handler=Status())
    app.route("/fuse/{project}/config.yaml", virtual=True, handler=Status())
    return app


def test_parse_pattern_kinds() -> None:
    segs = parse_pattern("/a/{b}/{c*}")
    assert [s.kind for s in segs] == [Kind.LITERAL, Kind.PARAM, Kind.REST]
    assert [str(s) for s in segs] == ["a", "{b}", "{c*}"]


@pytest.mark.parametrize(
    ("pattern", "match"),
    [
        ("/a/{rest*}/b", "last segment"),
        ("/a/x{b}", "whole segment"),
        ("/a/{1x}", "not a valid param"),
        ("/a/..", "not a valid segment"),
    ],
)
def test_parse_pattern_errors(pattern: str, match: str) -> None:
    with pytest.raises(RouteError, match=match):
        parse_pattern(pattern)


def test_own_backend_with_tail() -> None:
    app = Sluice()
    app.route("/root/{project}", backend="/data/{project}")
    res = app.freeze().resolve("/root/p/sub/file.txt")
    assert res is not None
    assert res.kind == "backed"
    assert res.real_path == Path("/data/p/sub/file.txt")
    assert res.backend_root == Path("/data/p")
    assert res.params == {"project": "p"}
    assert res.tail == ("sub", "file.txt")


def test_inherited_backend_measures_tail_from_owner() -> None:
    res = tenant_app().freeze().resolve("/fuse/alice/T0k/private/notes.txt")
    assert res is not None
    assert res.kind == "backed"
    assert res.real_path == Path("/ext4/alice/private/notes.txt")
    assert res.route.full_pattern == "/fuse/{user}/{token}/private"
    assert res.owner is not None and res.owner.full_pattern == "/fuse/{user}/{token}"
    assert len(res.middlewares) == 2  # the owner's and private's


def test_virtual_stops_inheritance() -> None:
    res = tenant_app().freeze().resolve("/fuse/alice/T0k/status")
    assert res is not None
    assert res.virtual
    assert res.real_path is None
    assert isinstance(res.handler, Status)


def test_literal_beats_param() -> None:
    table = tenant_app().freeze()
    res = table.resolve("/fuse/p/config.yaml")
    assert res is not None and res.virtual
    assert res.params == {"project": "p"}
    other = table.resolve("/fuse/p/other.yaml")
    assert other is not None and other.kind == "backed"
    assert other.real_path == Path("/ext4/p")


def test_param_beats_rest_and_longer_beats_prefix() -> None:
    app = Sluice()
    app.route("/a/{x}", backend="/one/{x}")
    app.route("/a/{x}/b", backend="/two/{x}")
    app.route("/c/{rest*}", backend="/rest/{rest}")
    app.route("/c/{y}", backend="/param/{y}")
    table = app.freeze()
    res = table.resolve("/a/1/b")
    assert res is not None and res.real_path == Path("/two/1")
    res = table.resolve("/c/q")
    assert res is not None and res.real_path == Path("/param/q")
    res = table.resolve("/c/q/r")
    assert res is not None and res.real_path == Path("/param/q/r")


def test_rest_captures_many_segments() -> None:
    app = Sluice()
    app.route("/r/{path*}", backend="/srv/{path}")
    res = app.freeze().resolve("/r/x/y/z")
    assert res is not None
    assert res.params == {"path": "x/y/z"}
    assert res.real_path == Path("/srv/x/y/z")


def test_synthetic_directories_and_listing() -> None:
    table = tenant_app().freeze()
    root = table.resolve("/")
    assert root is not None and root.kind == "synthetic"
    assert root.children == ("fuse",)
    fuse = table.resolve("/fuse")
    assert fuse is not None and fuse.kind == "synthetic"
    assert fuse.children == ()  # params can't be listed
    alice = table.resolve("/fuse/alice")
    assert alice is not None and alice.kind == "synthetic"
    assert alice.children == ("config.yaml",)
    backed = table.resolve("/fuse/alice/T0k")
    assert backed is not None and backed.kind == "backed"
    assert backed.children == ("private", "status")


def test_unmatched_path_is_none() -> None:
    table = tenant_app().freeze()
    assert table.resolve("/nothing") is None
    assert table.resolve("/fuse/alice/config.yaml/deeper") is not None  # virtual, with a tail


def test_grouping_node_middleware_applies_beneath() -> None:
    app = Sluice()
    group = app.route("/g", middlewares=[ok])
    group.route("data", backend="/d")
    table = app.freeze()
    res = table.resolve("/g/data/x")
    assert res is not None and res.real_path == Path("/d/x")
    assert len(res.middlewares) == 1
    g = table.resolve("/g")
    assert g is not None and g.kind == "synthetic"
    assert table.resolve("/g/other") is None


def test_secret_params_are_masked_in_display_path() -> None:
    app = tenant_app()
    app.param("token", decode, secret=True)
    res = app.freeze().resolve("/fuse/alice/T0k/notes.txt")
    assert res is not None
    assert res.display_path == "/fuse/alice/********/notes.txt"
    assert [p.name for p in res.param_specs] == ["token"]


def test_env_in_backend_template() -> None:
    app = Sluice()
    app.route("/e/{x}", backend="/{env.REGION}/{x}")
    table = app.freeze(MountConfig(env={"REGION": "eu"}))
    res = table.resolve("/e/a")
    assert res is not None and res.real_path == Path("/eu/a")


@pytest.mark.parametrize("path", ["/r/..", "/r/a/../../etc", "/s/../../../etc"])
def test_dotdot_cannot_escape_the_backend(path: str) -> None:
    app = Sluice()
    app.route("/r/{x}", backend="/srv/{x}")
    app.route("/s/{rest*}", backend="/srv/u-{rest}")
    with pytest.raises(FsError, match="escapes"):
        app.freeze().resolve(path)


def test_dotdot_within_the_backend_is_fine() -> None:
    app = Sluice()
    app.route("/r/{x}", backend="/srv/{x}")
    res = app.freeze().resolve("/r/a/b/..")
    assert res is not None and res.real_path == Path("/srv/a")


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda a: a.route("/x/{u}", backend="/b/{v}"), "no route captures"),
        (lambda a: a.route("/x/{u}", backend="/b/{env.NOPE}"), "not in env.expose"),
        (lambda a: a.route("/x/{u}", backend="/b/{secret.k}"), "can't use"),
        (lambda a: a.route("/x/{u}", backend="relative/{u}"), "absolute"),
        (lambda a: a.route("/x/{u}", backend="/b/{u"), "unbalanced"),
        (lambda a: a.route("/x/{u}", backend="/b/{a-b}"), "invalid placeholder"),
        (lambda a: a.route("/x/{u}/{u}"), "more than once"),
        (lambda a: a.route("/x/{u}").route("{u}"), "more than once"),
        (lambda a: a.route("/v", virtual=True), "needs a handler"),
        (lambda a: a.route("/v", virtual=True, handler=Status(), backend="/b"), "can't have"),
        (lambda a: a.route("/v", handler=Status()), "needs virtual=True"),
        (lambda a: a.route("/v", virtual=True, handler=object()), "VirtualHandler"),
        (lambda a: (a.route("/a/{x}"), a.route("/a/{y}")), "same shape"),
        (lambda a: a.route("/m", middlewares=[lambda ctx: None]), "async"),
        (lambda a: a.route("/m", middlewares=[42]), "not middleware"),
        (lambda a: a.param("p", lambda ctx, raw: raw), "async"),
    ],
)
def test_freeze_validation(build: Callable[[Sluice], object], match: str) -> None:
    app = Sluice()
    build(app)
    with pytest.raises(RouteError, match=match):
        app.freeze()


def test_secret_param_in_backend_is_refused() -> None:
    app = Sluice()
    app.route("/x/{token}", backend="/b/{token}")
    app.param("token", secret=True)
    with pytest.raises(RouteError, match="secret param"):
        app.freeze()


def test_walk_reports_backend_states() -> None:
    states = [(d, n.route.full_pattern, s) for d, n, s in tenant_app().freeze().walk()]
    assert states == [
        (0, "/fuse/{user}/{token}", "backend /ext4/{user}"),
        (1, "/fuse/{user}/{token}/private", "inherits /ext4/{user} from /fuse/{user}/{token}"),
        (1, "/fuse/{user}/{token}/status", "virtual"),
        (0, "/fuse/{project}/config.yaml", "virtual"),
    ]


def test_walk_grouping() -> None:
    app = Sluice()
    app.route("/g").route("v", virtual=True, handler=Status()).route("under")
    assert [s for _, _, s in app.freeze().walk()] == ["grouping", "virtual", "grouping"]
