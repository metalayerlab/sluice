import asyncio
import errno
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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


# Each run_pipeline rule, checked on every side of a call: `alone` (no `also`), `source` (the
# primary context of a two-path op such as rename) and `target` (its `also` context). The tested
# side's events must be identical on every side; #26 and #27 were rules that held on the source
# chain only.

TESTED, BYSTANDER = "/t/x/f", "/o/f"


class Probe(Recorder):
    """A Recorder that flags any hook run with another side's context."""

    def __init__(
        self, name: str, log: list[str], ops: frozenset[str] | None = None, where: str = TESTED
    ) -> None:
        super().__init__(name, log, ops)
        self.where = where

    def note(self, ctx: Context, event: str) -> None:
        wrong = "" if ctx.display_path == self.where else f"@{ctx.display_path}"
        self.log.append(f"{event}:{self.name}{wrong}")

    async def pre(self, ctx: Context) -> None:
        self.note(ctx, "pre")

    async def post(self, ctx: Context, result: Any) -> Any:
        self.note(ctx, "post")
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        self.note(ctx, f"failed:{errno.errorcode[error.errno]}")


class Answer(Probe):
    async def pre(self, ctx: Context) -> None:
        await super().pre(ctx)
        ctx.respond(b"answered")


class Gate(Probe):
    async def pre(self, ctx: Context) -> None:
        await super().pre(ctx)
        raise Deny()


class Boom(Probe):
    async def pre(self, ctx: Context) -> None:
        await super().pre(ctx)
        raise RuntimeError("bug")


class Upper(Probe):
    async def post(self, ctx: Context, result: Any) -> Any:
        await super().post(ctx, result)
        return result.upper()


class BrokenPost(Probe):
    async def post(self, ctx: Context, result: Any) -> Any:
        await super().post(ctx, result)
        raise ValueError("redaction failed")


class NoisyFailed(Probe):
    async def failed(self, ctx: Context, error: FsError) -> None:
        await super().failed(ctx, error)
        raise RuntimeError("in failed")


@dataclass(frozen=True)
class Rule:
    chain: Callable[[list[str]], list[object]]
    events: list[str]  # the tested side's: its hooks, its param() callback and the operation
    result: bytes = b"op"
    error: int | None = None  # the errno the call fails with; `result` is then unused
    param: str | None = None  # the param() callback: "upper", or "deny" / "crash" to raise
    raises: BaseException | None = None  # what the operation raises
    stops_in_pre: bool = False  # the tested chain ends the call before the operation
    post_fails: bool = False


def _look(log: list[str]) -> Callable[[Context], Awaitable[None]]:
    async def look(ctx: Context) -> None:
        log.append(f"look:{ctx.params['p']}")

    return look


RULES = {
    "pre-in-order-post-in-reverse": Rule(
        lambda log: [Probe("a", log), Probe("b", log)],
        ["pre:a", "pre:b", "op", "post:b", "post:a"],
    ),
    "post-replaces-the-result": Rule(
        lambda log: [Probe("a", log), Upper("u", log)],
        ["pre:a", "pre:u", "op", "post:u", "post:a"],
        result=b"OP",
    ),
    "ops-filter-skips-middleware": Rule(
        lambda log: [Probe("w", log, frozenset({"write"})), Probe("a", log)],
        ["pre:a", "op", "post:a"],
    ),
    "param-transforms-before-pre": Rule(
        lambda log: [_look(log), Probe("a", log)],
        ["param:x", "look:X", "pre:a", "op", "post:a"],
        param="upper",
    ),
    "respond-skips-later-pre-and-the-op": Rule(
        lambda log: [Probe("a", log), Answer("r", log), Probe("b", log)],
        ["pre:a", "pre:r", "post:r", "post:a"],
        result=b"answered",
        stops_in_pre=True,
    ),
    "deny-is-eacces-to-the-run-hooks": Rule(
        lambda log: [Probe("a", log), Gate("g", log), Probe("b", log)],
        ["pre:a", "pre:g", "failed:EACCES:g", "failed:EACCES:a"],
        error=errno.EACCES,
        stops_in_pre=True,
    ),
    "param-failure-reaches-the-whole-chain": Rule(
        lambda log: [Probe("a", log), Probe("b", log)],
        ["param:x", "failed:ENOENT:b", "failed:ENOENT:a"],
        error=errno.ENOENT,
        param="deny",
        stops_in_pre=True,
    ),
    "unexpected-param-error-is-eio": Rule(
        lambda log: [Probe("a", log)],
        ["param:x", "failed:EIO:a"],
        error=errno.EIO,
        param="crash",
        stops_in_pre=True,
    ),
    "unexpected-pre-error-is-eio": Rule(
        lambda log: [Probe("a", log), Boom("x", log), Probe("b", log)],
        ["pre:a", "pre:x", "failed:EIO:x", "failed:EIO:a"],
        error=errno.EIO,
        stops_in_pre=True,
    ),
    "fs-error-from-the-op-passes-through": Rule(
        lambda log: [Probe("a", log)],
        ["pre:a", "op", "failed:ENOSPC:a"],
        error=errno.ENOSPC,
        raises=FsError(errno.ENOSPC),
    ),
    "oserror-from-the-op-keeps-its-errno": Rule(
        lambda log: [Probe("a", log)],
        ["pre:a", "op", "failed:ENOENT:a"],
        error=errno.ENOENT,
        raises=FileNotFoundError(errno.ENOENT, "gone"),
    ),
    "oserror-without-errno-is-eio": Rule(
        lambda log: [Probe("a", log)],
        ["pre:a", "op", "failed:EIO:a"],
        error=errno.EIO,
        raises=OSError("no errno"),
    ),
    "unexpected-op-error-is-eio": Rule(
        lambda log: [Probe("a", log)],
        ["pre:a", "op", "failed:EIO:a"],
        error=errno.EIO,
        raises=RuntimeError("bug"),
    ),
    "post-failure-is-eio-to-every-run-hook": Rule(
        lambda log: [Probe("a", log), BrokenPost("x", log)],
        ["pre:a", "pre:x", "op", "post:x", "failed:EIO:x", "failed:EIO:a"],
        error=errno.EIO,
        post_fails=True,
    ),
    "failed-hook-error-is-swallowed": Rule(
        lambda log: [Probe("a", log), NoisyFailed("n", log)],
        ["pre:a", "pre:n", "op", "failed:ENOSPC:n", "failed:ENOSPC:a"],
        error=errno.ENOSPC,
        raises=FsError(errno.ENOSPC),
    ),
}


def _param(log: list[str], mode: str) -> Callable[[Context, Any], Awaitable[Any]]:
    async def func(ctx: Context, raw: Any) -> Any:
        log.append(f"param:{raw}")
        if mode == "deny":
            raise FsError(errno.ENOENT, "no such p")
        if mode == "crash":
            raise RuntimeError("bug")
        return raw.upper()

    return func


@pytest.mark.parametrize("side", ["alone", "source", "target"])
@pytest.mark.parametrize("rule", RULES.values(), ids=RULES.keys())
def test_each_pipeline_rule_holds_on_every_side(rule: Rule, side: str) -> None:
    log: list[str] = []
    other: list[str] = []
    app = Sluice()
    if rule.param:
        app.param("p", _param(log, rule.param))
    app.route("/t/{p}", backend="/b", middlewares=rule.chain(log))
    app.route("/o", backend="/o", middlewares=[Probe("o", other, where=BYSTANDER)])
    table = app.freeze()
    tested, bystander = context(table, TESTED, "rename"), context(table, BYSTANDER, "rename")

    async def operation(ctx: Context) -> bytes:
        log.append("op")
        if rule.raises is not None:
            raise rule.raises
        return b"op"

    primary, also = {
        "alone": (tested, []),
        "source": (tested, [bystander]),
        "target": (bystander, [tested]),
    }[side]
    if rule.error is None:
        assert run(primary, operation, also=also) == rule.result
    else:
        with pytest.raises(FsError) as e:
            run(primary, operation, also=also)
        assert e.value.errno == rule.error
    assert log == rule.events

    # The bystander chain ran pre unless the tested source chain ended the call first.
    outcome = ["post:o"] if rule.error is None else [f"failed:{errno.errorcode[rule.error]}:o"]
    if side == "source" and rule.post_fails:
        outcome = ["post:o", *outcome]  # post runs in reverse: the target's before the source's
    expected = {
        "alone": [],
        "source": [] if rule.stops_in_pre else ["pre:o", *outcome],
        "target": ["pre:o", *outcome],
    }[side]
    assert other == expected


def test_unexpected_error_in_pre_is_eio_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom(ctx: Context) -> None:
        raise RuntimeError("bug")

    with pytest.raises(FsError) as e:
        run(context(make(boom)))
    assert e.value.errno == errno.EIO
    assert "unexpected error in pre [c1]" in caplog.text
    assert "T0k" in caplog.text  # not a secret param here


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


def test_also_target_respond_short_circuits_the_operation() -> None:
    log: list[str] = []

    class Answer(PathRecorder):
        async def pre(self, ctx: Context) -> None:
            await super().pre(ctx)
            ctx.respond("answered")

    app = Sluice()
    app.route("/a", backend="/a", middlewares=[PathRecorder("src", log)])
    app.route("/b", backend="/b", middlewares=[Answer("dst", log), PathRecorder("late", log)])
    src, dst = rename_contexts(app.freeze(), "/a/f", "/b/f")

    async def op(ctx: Context) -> None:
        log.append("op")

    assert run(src, op, also=[dst]) == "answered"
    assert log == ["pre:src:/a/f", "pre:dst:/b/f", "post:dst:/b/f", "post:src:/a/f"]


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
