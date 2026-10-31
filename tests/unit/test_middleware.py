import asyncio
import errno
import logging
from typing import Any

import pytest

from sluice import Context, Deny, FsError, Middleware, MountConfig, Sluice, access_log, auth
from sluice.middleware import run_pipeline
from sluice.routing import RouteTable


class Recorder(Middleware):
    def __init__(self, name: str, log: list[str], ops: frozenset[str] | None = None) -> None:
        self.name = name
        self.log = log
        self.ops = ops

    async def pre(self, ctx: Context) -> None:
        self.log.append(f"pre:{self.name}")

    async def post(self, ctx: Context, result: Any) -> Any:
        self.log.append(f"post:{self.name}")
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        self.log.append(f"failed:{self.name}:{errno.errorcode[error.errno]}")


def make(*middlewares: object, params: dict[str, Any] | None = None) -> RouteTable:
    app = Sluice()
    for name, (func, secret) in (params or {}).items():
        app.param(name, func, secret=secret)
    app.route("/t/{user}/{token}", backend="/b/{user}", middlewares=list(middlewares))
    return app.freeze()


def context(table: RouteTable, path: str = "/t/alice/T0k/f", op: str = "read") -> Context:
    res = table.resolve(path)
    assert res is not None
    return Context(op, res, config=MountConfig(), correlation_id="c1")


def run(ctx: Context, operation: Any = None, **kw: Any) -> Any:
    async def default(ctx: Context) -> bytes:
        return b"data"

    return asyncio.run(run_pipeline(ctx, operation or default, **kw))


def test_order_pre_then_operation_then_post_in_reverse() -> None:
    log: list[str] = []

    async def op(ctx: Context) -> bytes:
        log.append("op")
        return b"x"

    ctx = context(make(Recorder("a", log), Recorder("b", log)))
    assert run(ctx, op) == b"x"
    assert log == ["pre:a", "pre:b", "op", "post:b", "post:a"]


def test_post_replaces_result() -> None:
    class Upper(Middleware):
        async def post(self, ctx: Context, result: Any) -> Any:
            return result.upper()

    assert run(context(make(Upper()))) == b"DATA"


def test_respond_short_circuits() -> None:
    log: list[str] = []

    class Cached(Middleware):
        async def pre(self, ctx: Context) -> None:
            ctx.respond(b"cached")

    async def op(ctx: Context) -> bytes:
        log.append("op")
        return b"x"

    ctx = context(make(Recorder("a", log), Cached(), Recorder("b", log)))
    assert run(ctx, op) == b"cached"
    assert log == ["pre:a", "post:a"]


def test_deny_in_pre_runs_failed_in_reverse_including_raiser() -> None:
    log: list[str] = []

    class Gate(Recorder):
        async def pre(self, ctx: Context) -> None:
            await super().pre(ctx)
            raise Deny()

    ctx = context(make(Recorder("a", log), Gate("g", log), Recorder("b", log)))
    with pytest.raises(FsError) as e:
        run(ctx)
    assert e.value.errno == errno.EACCES
    assert log == ["pre:a", "pre:g", "failed:g:EACCES", "failed:a:EACCES"]


def test_oserror_from_operation_keeps_its_errno() -> None:
    async def op(ctx: Context) -> None:
        raise FileNotFoundError(errno.ENOENT, "gone")

    with pytest.raises(FsError) as e:
        run(context(make()), op)
    assert e.value.errno == errno.ENOENT


def test_unexpected_error_in_pre_is_eio_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom(ctx: Context) -> None:
        raise RuntimeError("bug")

    with pytest.raises(FsError) as e:
        run(context(make(boom)))
    assert e.value.errno == errno.EIO
    assert "unexpected error in pre [c1]" in caplog.text
    assert "T0k" in caplog.text  # not a secret param here


def test_post_fails_closed() -> None:
    log: list[str] = []

    class Broken(Middleware):
        async def post(self, ctx: Context, result: Any) -> Any:
            raise ValueError("redaction failed")

    ctx = context(make(Recorder("a", log), Broken()))
    with pytest.raises(FsError) as e:
        run(ctx)
    assert e.value.errno == errno.EIO
    assert log == ["pre:a", "failed:a:EIO"]


def test_failed_hook_errors_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    class Noisy(Middleware):
        async def failed(self, ctx: Context, error: FsError) -> None:
            raise RuntimeError("in failed")

    async def op(ctx: Context) -> None:
        raise FsError(errno.ENOSPC)

    with pytest.raises(FsError) as e:
        run(context(make(Noisy())), op)
    assert e.value.errno == errno.ENOSPC
    assert "ignored" in caplog.text


def test_ops_filter() -> None:
    log: list[str] = []
    ctx = context(make(Recorder("w", log, frozenset({"write"}))), op="read")
    run(ctx)
    assert log == []


def test_param_callbacks_transform_and_run_before_pre() -> None:
    seen: list[object] = []

    async def decode(ctx: Context, raw: Any) -> Any:
        return {"token": raw}

    async def look(ctx: Context) -> None:
        seen.append(ctx.params["token"])

    ctx = context(make(look, params={"token": (decode, True)}))
    run(ctx)
    assert seen == [{"token": "T0k"}]


def test_param_denial_lets_the_chain_observe_it() -> None:
    log: list[str] = []

    async def reject(ctx: Context, raw: Any) -> Any:
        raise FsError(errno.ENOENT, "no such user")

    ctx = context(make(Recorder("a", log), params={"user": (reject, False)}))
    with pytest.raises(FsError):
        run(ctx)
    assert log == ["failed:a:ENOENT"]


class PathRecorder(Recorder):
    async def pre(self, ctx: Context) -> None:
        self.log.append(f"pre:{self.name}:{ctx.display_path}")

    async def post(self, ctx: Context, result: Any) -> Any:
        self.log.append(f"post:{self.name}:{ctx.display_path}")
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        self.log.append(f"failed:{self.name}:{ctx.display_path}:{errno.errorcode[error.errno]}")


def rename_contexts(table: RouteTable, source: str, target: str) -> tuple[Context, Context]:
    return context(table, source, "rename"), context(table, target, "rename")


def test_also_runs_shared_middleware_against_the_target_too() -> None:
    log: list[str] = []
    app = Sluice()
    app.use(PathRecorder("shared", log))
    app.route("/a", backend="/a", middlewares=[PathRecorder("src", log)])
    app.route("/b", backend="/b", middlewares=[PathRecorder("dst", log)])
    src, dst = rename_contexts(app.freeze(), "/a/f", "/b/f")

    async def op(ctx: Context) -> None:
        log.append("op")

    run(src, op, also=[dst])
    assert log == [
        "pre:shared:/a/f",
        "pre:src:/a/f",
        "pre:shared:/b/f",
        "pre:dst:/b/f",
        "op",
        "post:dst:/b/f",
        "post:shared:/b/f",
        "post:src:/a/f",
        "post:shared:/a/f",
    ]


def test_also_target_middleware_observes_a_failed_operation() -> None:
    log: list[str] = []
    app = Sluice()
    app.route("/a", backend="/a", middlewares=[PathRecorder("src", log)])
    app.route("/b", backend="/b", middlewares=[PathRecorder("dst", log)])
    src, dst = rename_contexts(app.freeze(), "/a/f", "/b/f")

    async def op(ctx: Context) -> None:
        raise FsError(errno.ENOSPC)

    with pytest.raises(FsError):
        run(src, op, also=[dst])
    assert log == [
        "pre:src:/a/f",
        "pre:dst:/b/f",
        "failed:dst:/b/f:ENOSPC",
        "failed:src:/a/f:ENOSPC",
    ]


def test_also_target_param_denial_lets_the_target_chain_observe_it() -> None:
    log: list[str] = []

    async def reject(ctx: Context, raw: Any) -> Any:
        raise FsError(errno.ENOENT, "no such user")

    app = Sluice()
    app.route("/a", backend="/a", middlewares=[PathRecorder("src", log)])
    app.route("/b/{user}", backend="/b", middlewares=[PathRecorder("dst", log)])
    app.param("user", reject)
    src, dst = rename_contexts(app.freeze(), "/a/f", "/b/x/f")
    with pytest.raises(FsError):
        run(src, also=[dst])
    assert log == [
        "pre:src:/a/f",
        "failed:dst:/b/x/f:ENOENT",
        "failed:src:/a/f:ENOENT",
    ]


def test_rename_within_one_route_is_authorised_against_the_target() -> None:
    seen: list[str] = []

    async def verify(user: Any, token: Any, real_path: Any, op: str) -> bool:
        seen.append(str(real_path))
        return "locked" not in str(real_path)

    app = Sluice()
    app.use(auth(verify))
    app.route("/b", backend="/srv/b")
    src, dst = rename_contexts(app.freeze(), "/b/a/f", "/b/locked/f")
    with pytest.raises(FsError) as e:
        run(src, also=[dst])
    assert e.value.errno == errno.EACCES
    assert seen == ["/srv/b/a/f", "/srv/b/locked/f"]


def test_auth_default_allows_and_injected_verify_denies() -> None:
    run(context(make(auth())))
    calls: list[tuple[object, ...]] = []

    async def verify(user: Any, token: Any, real_path: Any, op: str) -> bool:
        calls.append((user, token, str(real_path), op))
        return False

    with pytest.raises(FsError) as e:
        run(context(make(auth(verify))))
    assert e.value.errno == errno.EACCES
    assert calls == [("alice", "T0k", "/b/alice/f", "read")]


def test_auth_refuses_sync_verify() -> None:
    from sluice import RouteError

    with pytest.raises(RouteError, match="async"):
        auth(lambda *a: True)  # type: ignore[arg-type]  # the error under test


def test_access_log_masks_secret_segments(caplog: pytest.LogCaptureFixture) -> None:
    async def decode(ctx: Context, raw: Any) -> Any:
        return raw

    table = make(access_log(), params={"token": (decode, True)})
    with caplog.at_level(logging.INFO, logger="sluice.access"):
        run(context(table))

        async def fail(ctx: Context) -> None:
            raise FsError(errno.EPERM)

        with pytest.raises(FsError):
            run(context(table), fail)
    assert "read /t/alice/********/f" in caplog.text
    assert "-> ok" in caplog.text
    assert "-> EPERM" in caplog.text
    assert "T0k" not in caplog.text


def test_function_middleware_repr() -> None:
    async def gate(ctx: Context) -> None:
        return None

    table = make(gate, access_log(), auth())
    res = table.resolve("/t/a/b")
    assert res is not None
    names = [repr(m) for m in res.middlewares]
    assert names[0].endswith("gate")
    assert names[1:] == ["access_log()", "auth(allow_all)"]
