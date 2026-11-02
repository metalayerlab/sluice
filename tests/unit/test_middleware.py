import asyncio
import contextlib
import errno
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from sluice import Context, Deny, FsError, Middleware, MountConfig, Sluice
from sluice.metrics import Metrics
from sluice.middleware import POLICY, as_middleware, policy, run_pipeline
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


class Missing(Probe):
    async def pre(self, ctx: Context) -> None:
        await super().pre(ctx)
        raise FileNotFoundError(errno.ENOENT, "a hook's own lookup")


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
    "oserror-from-pre-keeps-its-errno": Rule(
        lambda log: [Probe("a", log), Missing("m", log), Probe("b", log)],
        ["pre:a", "pre:m", "failed:ENOENT:m", "failed:ENOENT:a"],
        error=errno.ENOENT,
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


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(socket.EAI_NONAME, "unknown host"),
        ssl.SSLError(1, "certificate verify failed"),
        OSError(99999, "not an errno"),
    ],
    ids=["gaierror", "sslerror", "unknown-errno"],
)
def test_library_oserrors_in_hooks_are_eio_and_logged(
    error: OSError, caplog: pytest.LogCaptureFixture
) -> None:
    async def hook(ctx: Context) -> None:
        raise error

    with pytest.raises(FsError) as e:
        run(context(make(hook)))
    assert e.value.errno == errno.EIO
    assert "unexpected error in pre" in caplog.text


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

    async def locked(ctx: Context) -> None:
        seen.append(str(ctx.real_path))
        if "locked" in str(ctx.real_path):
            raise Deny("locked")

    app = Sluice()
    app.use(locked)
    app.route("/b", backend="/srv/b")
    src, dst = rename_contexts(app.freeze(), "/b/a/f", "/b/locked/f")
    with pytest.raises(FsError) as e:
        run(src, also=[dst])
    assert e.value.errno == errno.EACCES
    assert seen == ["/srv/b/a/f", "/srv/b/locked/f"]


def test_function_middleware_repr() -> None:
    async def gate(ctx: Context) -> None:
        return None

    table = make(gate, Recorder("r", []))
    res = table.resolve("/t/a/b")
    assert res is not None
    names = [repr(m) for m in res.middlewares]
    assert names[0].endswith("gate")
    assert names[1:] == ["Recorder"]


# The interpose pipeline core (design 0004): built once per route, today's behaviour unchanged.


def test_policy_is_sluices_semantics() -> None:
    assert POLICY.on_error == "fail"
    assert POLICY.after_order == "reverse"
    assert POLICY.after_local is True
    assert POLICY.observe_failed is True
    assert POLICY.timeout is None  # pipeline.timeout_ms is off by default (design 0006)
    assert POLICY.answers == (FsError,)
    assert POLICY.skip_inherited is Middleware  # design 0008; see the tests below


def test_pipeline_is_frozen_per_route_and_shares_inherited_entries() -> None:
    async def gate(ctx: Context) -> None:
        return None

    app = Sluice()
    app.use(gate)
    home = app.route("/h/{user}", backend="/srv/{user}", middlewares=[Recorder("r", [])])
    home.route("private", middlewares=[Recorder("p", [])])
    table = app.freeze()
    a, b = table.resolve("/h/a/x"), table.resolve("/h/b/y")
    private = table.resolve("/h/a/private/z")
    assert a is not None and b is not None and private is not None
    assert a.pipeline is b.pipeline  # built at freeze time, not per call
    assert private.pipeline is not a.pipeline
    # The child's chain reuses its ancestors' entries: one entry per registration.
    assert private.pipeline.entries[:2] == a.pipeline.entries
    assert [e.middleware for e in private.pipeline.entries] == list(private.middlewares)
    # Named as `sluice routes` and the logs name them; a repeated name gets "#2" (design 0007).
    gate_name, r, p = (repr(m) for m in private.middlewares)
    assert r == p == "Recorder"
    assert [e.name for e in private.pipeline.entries] == [gate_name, "Recorder", "Recorder#2"]
    assert all(e.on_error is None and e.timeout is None for e in private.pipeline.entries)


def test_the_same_middleware_twice_runs_twice() -> None:
    log: list[str] = []
    twice = Recorder("t", log)
    assert run(context(make(twice, twice))) == b"data"
    assert log == ["pre:t", "pre:t", "post:t", "post:t"]


def test_a_hook_set_on_the_instance_runs() -> None:
    log: list[str] = []

    async def gate(ctx: Context) -> None:
        log.append("gate")
        raise Deny()

    mw = Middleware()
    mw.pre = gate  # type: ignore[method-assign]  # the instance attribute is what's tested
    with pytest.raises(FsError) as e:
        run(context(make(mw)))
    assert e.value.errno == errno.EACCES
    assert log == ["gate"]


def _responding_param(table_routes: Callable[[Sluice], None]) -> RouteTable:
    async def answer(ctx: Context, raw: Any) -> Any:
        ctx.respond(b"from-param")
        return raw

    app = Sluice()
    app.param("p", answer)
    table_routes(app)
    return app.freeze()


def test_a_respond_in_param_stops_after_the_first_pre() -> None:
    log: list[str] = []

    class PostOnly(Recorder):
        pre = Middleware.pre

    def routes(app: Sluice) -> None:
        chain = [PostOnly("a", log), Recorder("b", log)]
        app.route("/t/{p}", backend="/b", middlewares=chain)

    ctx = context(_responding_param(routes), "/t/x/f")
    assert run(ctx) == b"from-param"
    assert log == ["post:a"]


def test_a_respond_in_a_target_param_with_no_chain_does_not_skip_the_op() -> None:
    def routes(app: Sluice) -> None:
        app.route("/s", backend="/s")
        app.route("/t/{p}", backend="/b")

    table = _responding_param(routes)

    async def op(ctx: Context) -> bytes:
        return b"op"

    src, dst = context(table, "/s/f", "rename"), context(table, "/t/x/f", "rename")
    assert run(src, op, also=[dst]) == b"op"


def test_a_pre_failure_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    log: list[str] = []
    with pytest.raises(FsError) as e:
        run(context(make(Probe("a", log), Boom("x", log))))
    assert e.value.errno == errno.EIO
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in errors] == ["read /t/alice/T0k/f: unexpected error in pre [c1]"]
    assert errors[0].exc_info is not None and isinstance(errors[0].exc_info[1], RuntimeError)


def test_a_post_failure_is_logged_once_as_post(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(FsError) as e:
        run(context(make(BrokenPost("x", []))))
    assert e.value.errno == errno.EIO
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == ["read /t/alice/T0k/f: unexpected error in post [c1]"]


def test_a_deny_is_an_answer_and_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(FsError) as e:
        run(context(make(Gate("g", []))))
    assert e.value.errno == errno.EACCES
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_failed_hook_errors_name_the_hook_and_its_own_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Noisy(Middleware):
        async def failed(self, ctx: Context, error: FsError) -> None:
            raise Deny()  # an answer elsewhere, but `failed` can't change the outcome

    app = Sluice()
    app.route("/a", backend="/a")
    app.route("/b", backend="/b", middlewares=[Noisy()])
    table = app.freeze()

    def ctx(path: str, cid: str) -> Context:
        res = table.resolve(path)
        assert res is not None
        return Context("rename", res, config=MountConfig(), correlation_id=cid)

    src, dst = ctx("/a/f", "s"), ctx("/b/f", "d")

    async def op(ctx: Context) -> None:
        raise FsError(errno.ENOSPC)

    with pytest.raises(FsError) as e:
        run(src, op, also=[dst])
    assert e.value.errno == errno.ENOSPC
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == ["Noisy.failed raised [d]; ignored"]


def test_an_ops_filter_that_raises_escapes_the_call() -> None:
    class Odd(Middleware):
        def applies(self, op: str) -> bool:
            raise RuntimeError("bad filter")

    with pytest.raises(RuntimeError, match="bad filter"):
        run(context(make(Odd())))


# pipeline.timeout_ms (design 0006)


def test_policy_adds_the_timeout_in_seconds_and_nothing_else() -> None:
    assert policy(0) is POLICY
    assert policy() is POLICY
    timed = policy(250)
    assert timed.timeout == 0.25
    assert {**vars(timed), "timeout": None} == vars(POLICY)


def test_freeze_bounds_every_pipeline_with_the_timeout() -> None:
    app = Sluice()
    app.use(Recorder("g", []))
    app.route("/t/{user}", backend="/b/{user}")
    for timeout_ms, seconds in ((0, None), (40, 0.04)):
        res = app.freeze(timeout_ms=timeout_ms).resolve("/t/a/f")
        assert res is not None
        assert res.pipeline.policy.timeout == seconds


class Stuck(Recorder):
    """Records whether its `pre` was cancelled at its await."""

    async def pre(self, ctx: Context) -> None:
        self.log.append(f"pre:{self.name}")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.log.append("cancelled")
            raise


def timed(*middlewares: object, timeout_ms: int = 20) -> RouteTable:
    app = Sluice()
    app.route("/t/{user}/{token}", backend="/b/{user}", middlewares=list(middlewares))
    return app.freeze(timeout_ms=timeout_ms)


def test_a_hook_that_times_out_is_cancelled_and_fails_closed_with_eio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[str] = []
    ran: list[str] = []

    async def op(ctx: Context) -> bytes:
        ran.append("op")
        return b"data"

    with pytest.raises(FsError) as e:
        run(context(timed(Recorder("a", log), Stuck("s", log))), op)
    assert e.value.errno == errno.EIO
    assert ran == []  # the operation never runs
    # Cancelled at its await; then `failed` reaches every hook that ran, its own included.
    assert log == ["pre:a", "pre:s", "cancelled", "failed:s:EIO", "failed:a:EIO"]
    # One line, naming the hook and the limit; no traceback of the cancellation.
    lines = [r for r in caplog.records if "timed out" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].getMessage() == "read /t/alice/T0k/f: Stuck.pre timed out after 0.02s [c1]"
    assert lines[0].exc_info is None
    assert "unexpected error" not in caplog.text


def test_a_hook_within_the_timeout_is_unaffected() -> None:
    log: list[str] = []
    assert run(context(timed(Recorder("a", log), timeout_ms=5000))) == b"data"
    assert log == ["pre:a", "post:a"]


def test_without_a_timeout_a_slow_hook_finishes() -> None:
    class Slow(Recorder):
        async def pre(self, ctx: Context) -> None:
            await asyncio.sleep(0.05)
            self.log.append("slept")

    log: list[str] = []
    assert run(context(timed(Slow("s", log), timeout_ms=0))) == b"data"
    assert log == ["slept", "post:s"]


def test_a_failed_hook_that_times_out_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    class SlowObserver(Middleware):
        def __repr__(self) -> str:
            return "SlowObserver"

        async def failed(self, ctx: Context, error: FsError) -> None:
            await asyncio.sleep(10)

    async def op(ctx: Context) -> None:
        raise FsError(errno.ENOSPC)

    with pytest.raises(FsError) as e:
        run(context(timed(SlowObserver())), op)
    assert e.value.errno == errno.ENOSPC  # the outcome was already fixed
    assert "SlowObserver.failed timed out after 0.02s [c1]; ignored" in caplog.text


# Distinct names for repeated middleware in one chain (design 0007)


def test_repeated_names_in_a_chain_get_a_number() -> None:
    async def gate(ctx: Context) -> None:
        return None

    app = Sluice()
    app.use(gate)
    home = app.route("/h/{user}", backend="/srv/{user}", middlewares=[gate, Recorder("a", [])])
    home.route("deep", middlewares=[gate, Recorder("b", []), Recorder("c", [])])
    app.route("/o/{x}", backend="/srv/o", middlewares=[Recorder("d", [])])
    table = app.freeze()
    g = repr(as_middleware(gate, "test"))
    deep, other = table.resolve("/h/a/deep/f"), table.resolve("/o/x/f")
    home_res = table.resolve("/h/a/f")
    assert deep is not None and other is not None and home_res is not None
    assert [e.name for e in home_res.pipeline.entries] == [g, f"{g}#2", "Recorder"]
    assert [e.name for e in deep.pipeline.entries] == [
        g,
        f"{g}#2",
        "Recorder",
        f"{g}#3",
        "Recorder#2",
        "Recorder#3",
    ]
    # Ancestors' entries keep their names; another chain starts again at the plain name.
    assert deep.pipeline.entries[:3] == home_res.pipeline.entries
    assert [e.name for e in other.pipeline.entries] == [g, "Recorder"]


def test_a_numbered_name_already_taken_is_skipped() -> None:
    class Named(Middleware):
        def __init__(self, name: str) -> None:
            self.label = name

        def __repr__(self) -> str:
            return self.label

        async def pre(self, ctx: Context) -> None:
            return None

    table = make(Named("x"), Named("x#2"), Named("x"))
    res = table.resolve("/t/a/T/f")
    assert res is not None
    assert [e.name for e in res.pipeline.entries] == ["x", "x#2", "x#3"]


def test_repeated_middleware_keep_separate_timings_and_metric_series() -> None:
    class Slow(Middleware):
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        def __repr__(self) -> str:
            return "Slow"

        async def pre(self, ctx: Context) -> None:
            await asyncio.sleep(self.seconds)

    metrics = Metrics()
    app = Sluice()
    app.route("/t/{user}/{token}", backend="/b/{user}", middlewares=[Slow(0), Slow(0.03)])
    table = app.freeze(metrics=metrics)
    # The declared label set holds the numbered name, so counting it can't be refused.
    assert {"Slow", "Slow#2"} <= metrics.middleware.values
    assert run(context(table)) == b"data"
    seconds = {values[0]: value for values, value in metrics.middleware_seconds.samples().items()}
    assert set(seconds) == {"Slow", "Slow#2"}
    assert seconds["Slow#2"] >= 0.03 > seconds["Slow"]
    assert 'sluice_middleware_seconds_total{middleware="Slow#2"}' in metrics.render().decode()


# interpose 0.1.4: a result that comes back after the limit is a timeout too (design 0006)


@pytest.mark.parametrize(
    ("late", "why"),
    [
        ("swallowed", "it swallowed the cancellation"),
        ("overran", "it blocked the event loop past the limit"),
    ],
)
def test_a_late_result_fails_closed_and_says_why(
    late: str, why: str, caplog: pytest.LogCaptureFixture
) -> None:
    class Late(Middleware):
        def __repr__(self) -> str:
            return "Late"

        async def pre(self, ctx: Context) -> None:
            if late == "overran":
                time.sleep(0.05)  # Blocks the loop: the cancellation has nowhere to land
                return
            with contextlib.suppress(asyncio.CancelledError):  # What a hook must never do
                await asyncio.sleep(10)

    ran: list[str] = []

    async def op(ctx: Context) -> bytes:
        ran.append("op")
        return b"data"

    with pytest.raises(FsError) as e:
        run(context(timed(Late())), op)
    assert e.value.errno == errno.EIO
    assert ran == []  # the late result is discarded, never acted on
    expected = f"read /t/alice/T0k/f: Late.pre timed out after 0.02s ({why}) [c1]"
    assert expected in [r.getMessage() for r in caplog.records]


# skip_inherited (design 0008): hooks left as the base class's no-ops aren't called


def test_hooks_a_middleware_does_not_override_are_not_timed() -> None:
    class PreOnly(Middleware):
        def __repr__(self) -> str:
            return "PreOnly"

        async def pre(self, ctx: Context) -> None:
            return None

    class FailedOnly(Middleware):
        def __repr__(self) -> str:
            return "FailedOnly"

        async def failed(self, ctx: Context, error: FsError) -> None:
            return None

    metrics = Metrics()
    app = Sluice()
    app.route("/t/{user}/{token}", backend="/b/{user}", middlewares=[PreOnly(), FailedOnly()])
    assert run(context(app.freeze(metrics=metrics))) == b"data"
    timed = {values[0] for values in metrics.middleware_seconds.samples()}
    # FailedOnly's pre and post are the base no-ops: never called, so never timed.
    assert timed == {"PreOnly"}


def test_a_failed_only_middleware_still_observes_a_failure() -> None:
    seen: list[int] = []

    class FailedOnly(Middleware):
        async def failed(self, ctx: Context, error: FsError) -> None:
            seen.append(error.errno)

    async def op(ctx: Context) -> None:
        raise FsError(errno.ENOSPC)

    with pytest.raises(FsError):
        run(context(make(FailedOnly())), op)
    assert seen == [errno.ENOSPC]


def test_a_respond_in_param_still_skips_the_operation_with_a_post_only_chain() -> None:
    # Every pre is skipped here; the ctx.responded check must still run after each one.
    log: list[str] = []

    class PostOnly(Recorder):
        pre = Middleware.pre

    def routes(app: Sluice) -> None:
        app.route("/t/{p}", backend="/b", middlewares=[PostOnly("a", log), PostOnly("b", log)])

    ran: list[str] = []

    async def op(ctx: Context) -> bytes:
        ran.append("op")
        return b"op"

    assert run(context(_responding_param(routes), "/t/x/f"), op) == b"from-param"
    assert ran == []
    assert log == ["post:a"]
