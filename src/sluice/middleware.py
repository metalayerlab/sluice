"""Middleware and the pipeline that runs it.

One call: `param()` callbacks → `pre` hooks (chain order) → the operation → `post` hooks
(reverse order). `failed` hooks observe any failure. Every hook is `async`: one event loop serves
the whole mount, so a hook that blocks stalls every call. With `pipeline.timeout_ms`, a hook that
runs longer is cancelled at its current `await` (design 0006): hooks must be cancel-safe and
never swallow `CancelledError`.

The loop and the guard around each hook come from `interpose.pipeline` (design 0004), driven by
`POLICY`: sluice's own semantics, written down. Hook names, selection by `ops`, `param()`, the
errno a failure becomes and the log lines stay here.
"""

from __future__ import annotations

import errno
import inspect
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from interpose.pipeline import Entry, Failure, HookFailed, Pipeline, Policy

from sluice.errors import FsError, RouteError

if TYPE_CHECKING:
    from sluice.context import Context
    from sluice.routing import ParamSpec

log = logging.getLogger(__name__)

Operation = Callable[["Context"], Awaitable[Any]]

# The operations middleware sees. forget, release and releasedir bypass it: they are bookkeeping
# the kernel can't act on the failure of.
OPS = frozenset(
    {
        "lookup",
        "getattr",
        "setattr",
        "readlink",
        "mkdir",
        "rmdir",
        "unlink",
        "symlink",
        "rename",
        "open",
        "create",
        "read",
        "write",
        "flush",
        "fsync",
        "opendir",
        "readdir",
        "statfs",
        "access",
        "getxattr",
        "setxattr",
        "listxattr",
        "removexattr",
    }
)


class Middleware:
    """Subclass and override any of the three hooks; `ops` limits which operations it sees."""

    ops: frozenset[str] | None = None  # None: every op

    async def pre(self, ctx: Context) -> None:
        """Before the operation: inspect, replace `ctx.args`, raise to deny, or `ctx.respond()`."""

    async def post(self, ctx: Context, result: Any) -> Any:
        """After the operation: return `result`, or a replacement for it."""
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        """The call failed. Observe only: the outcome can't change."""

    def applies(self, op: str) -> bool:
        return self.ops is None or op in self.ops

    def __repr__(self) -> str:
        return type(self).__name__


class FunctionMiddleware(Middleware):
    """A plain `async def fn(ctx)` used as middleware: a `pre` hook only."""

    def __init__(self, fn: Callable[[Context], Awaitable[None]]) -> None:
        self.fn = fn

    async def pre(self, ctx: Context) -> None:
        await self.fn(ctx)

    def __repr__(self) -> str:
        return getattr(self.fn, "__qualname__", repr(self.fn))


# sluice's answers to interpose's pipeline questions (interpose design 0003): a failing hook
# fails the call (with an errno, below); post hooks run in reverse, also after a ctx.respond();
# `failed` reaches every hook that ran, its own failure included; an FsError (Deny) is a hook's
# answer, not a failure. No timeout here: `pipeline.timeout_ms` sets one (`policy()`, design
# 0006). No `skip_inherited`: it looks at the class only, so a hook set on the instance would be
# skipped (an authorisation `pre` among them), and a skipped `pre` isn't followed by the
# `ctx.responded` check (design 0004).
POLICY = Policy(
    on_error="fail",
    after_order="reverse",
    after_local=True,
    observe_failed=True,
    answers=(FsError,),
)

MwEntry = Entry[Middleware]
MwPipeline = Pipeline[Middleware]


def policy(timeout_ms: int = 0) -> Policy:
    """`POLICY` with `pipeline.timeout_ms` (0: no limit) as interpose's per-hook timeout.

    A hook that runs longer is cancelled at its current `await` and fails the call with `EIO`,
    like any other hook failure (design 0006).
    """
    return replace(POLICY, timeout=timeout_ms / 1000) if timeout_ms else POLICY


def entry(mw: Middleware, taken: Collection[str] = ()) -> MwEntry:
    """One registration of a middleware in the route tree, built once at freeze time.

    Named by its `repr`, the name `sluice routes` and the logs already use (a class name, or a
    plain function's qualified name). No per-middleware `on_error` or timeout: the project's.
    `taken` are the names already in the chain: a second `auth` is `auth#2`, a third `auth#3`,
    so each keeps its own timings and metric series (design 0007).
    """
    base = repr(mw) or type(mw).__name__
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}#{n}"
    return Entry(mw, name)


def pipeline(entries: Sequence[MwEntry], rules: Policy = POLICY) -> MwPipeline:
    return Pipeline(entries, rules)


def check_coroutine(fn: object, where: str) -> None:
    if not inspect.iscoroutinefunction(fn):
        raise RouteError(f"{where}: {fn!r} must be an async function (async def)")


def as_middleware(obj: object, where: str) -> Middleware:
    """Validate a registered middleware, wrapping a plain coroutine function."""
    if isinstance(obj, Middleware):
        for hook in ("pre", "post", "failed"):
            check_coroutine(getattr(obj, hook), f"{where}: {obj!r}.{hook}")
        unknown = sorted((obj.ops or frozenset()) - OPS)
        if unknown:
            raise RouteError(f"{where}: {obj!r}.ops has unknown ops: {', '.join(unknown)}")
        return obj
    if callable(obj):
        check_coroutine(obj, where)
        return FunctionMiddleware(obj)
    raise RouteError(f"{where}: {obj!r} is not middleware")


# OSErrors whose errno is a library's own code, not the kernel's: getaddrinfo's are negative,
# and SSL_ERROR_SSL (1) would reach the caller as EPERM.
_NOT_FS_ERRORS = (socket.gaierror, socket.herror, ssl.SSLError)


def _fs_errno(e: Exception) -> int | None:
    """The kernel errno an OSError carries: a real filesystem answer wherever it came from (a
    hook's own I/O, say), so ENOENT from a hook doesn't surface as EIO."""
    if not isinstance(e, OSError) or isinstance(e, _NOT_FS_ERRORS):
        return None
    code = e.errno
    return code if isinstance(code, int) and code > 0 and code in errno.errorcode else None


def _as_fs_error(ctx: Context, e: Exception, stage: str) -> FsError:
    if isinstance(e, FsError):
        return e
    code = _fs_errno(e)
    if code is not None:
        return FsError(code, getattr(e, "strerror", None) or "")
    log.error(
        "%s %s: unexpected error in %s [%s]",
        ctx.op,
        ctx.display_path,
        stage,
        ctx.correlation_id,
        exc_info=e,
    )
    return FsError(errno.EIO)


# Why a hook's result came back after its limit (interpose 0.1.4's `Failure.late`): it never
# reached an `await` the cancellation could land on, or it caught the cancellation and returned.
_LATE = {
    "overran": " (it blocked the event loop past the limit)",
    "swallowed": " (it swallowed the cancellation)",
}


def _timed_out(failure: Failure) -> str:
    """`<middleware>.<hook> timed out after <s>s`, and why its result was late, if it was."""
    late = _LATE.get(failure.late or "", "")
    return f"{failure.middleware}.{failure.hook} timed out after {failure.timeout:g}s{late}"


class _Report:
    """`on_failure` for one call. Every failure is counted (with metrics on). A failure under
    "fail" ends the call and is logged once, by `_as_fs_error`; only one the pipeline carried on
    from (a `failed` hook's) is logged here, with the context of the hook that raised it."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.metrics = ctx.resolution.metrics

    def __call__(self, failure: Failure) -> None:
        if self.metrics is not None:
            self.metrics.failure(failure)
        if failure.action == "skip" and failure.timed_out:
            log.error("%s [%s]; ignored", _timed_out(failure), self.ctx.correlation_id)
        elif failure.action == "skip":
            log.error(
                "%s.%s raised [%s]; ignored",
                failure.middleware,
                failure.hook,
                self.ctx.correlation_id,
                exc_info=failure.error,
            )


def _selected(ctx: Context) -> list[MwEntry]:
    # Selected here rather than by `start(select=)`, so an `ops` filter that raises does what it
    # always did (escapes the call) instead of becoming a hook failure.
    return [e for e in ctx.resolution.pipeline.entries if e.middleware.applies(ctx.op)]


def _responded(ctx: Context) -> Callable[[object], bool]:
    return lambda _: ctx.responded


async def _pre(mw: Middleware, ctx: Context) -> None:
    await mw.pre(ctx)


async def _post(mw: Middleware, ctx: Context, result: Any) -> Any:
    return await mw.post(ctx, result)


async def run_params(ctx: Context, specs: Sequence[ParamSpec]) -> None:
    """Run `param()` callbacks: each replaces its own key in `ctx.params`."""
    for spec in specs:
        if spec.func is not None:
            ctx.params[spec.name] = await spec.func(ctx, ctx.params[spec.name])


async def run_pipeline(
    ctx: Context,
    operation: Operation,
    *,
    also: Sequence[Context] = (),
) -> Any:
    """Run `ctx`'s middleware chain around `operation`. Raises `FsError` on any failure.

    `also` are contexts for a second path (rename's target): before the operation, their
    `param()` callbacks and their whole chain's `pre` hooks run too, with that context. A
    middleware on both chains runs once per path, so an authorisation hook checks the target
    as well.
    A `ctx.respond()` on either side stops the remaining `pre` hooks and skips the operation.
    Every hook that ran `pre` gets `post` or `failed` with the same context.

    The loop and the guard around each hook are interpose's (`Pipeline`, frozen per route);
    what a failure means (an errno), selection by `ops`, `param()` and logging stay here.
    """
    chain = _selected(ctx)
    report = _Report(ctx)
    run = ctx.resolution.pipeline.start(on_failure=report, state=ctx.state)
    # Who `failed` reaches when it isn't everything that ran (`run.ran`): a param() failure
    # comes before its chain's pre hooks, so the whole chain observes it, as well as the hooks
    # that already ran.
    observers: list[tuple[MwEntry, Context]] | None = [(e, ctx) for e in chain]
    stage = "param()"
    try:
        await run_params(ctx, ctx.resolution.param_specs)
        stage, observers = "pre", None
        await run.before("pre", ctx, _pre, entries=chain, until=_responded(ctx))
        responder = ctx if ctx.responded else None
        for other in also:
            if responder is not None:
                break
            other_chain = _selected(other)
            stage, observers = "param()", [*run.ran, *((e, other) for e in other_chain)]
            await run_params(other, other.resolution.param_specs)
            stage, observers = "pre", None
            # Only a respond() seen after a pre hook ends the call here, as it always has: a
            # target chain with no middleware never checks.
            stopped = await run.before(
                "pre", other, _pre, entries=other_chain, until=_responded(other)
            )
            if stopped is not None:
                responder = other
        if responder is None:
            stage = "operation"
            result = await operation(ctx)
        else:
            result = responder.response
        stage = "post"
        result = await run.after("post", result, _post)
    except Exception as e:
        # A hook's failure arrives as HookFailed: map what the hook raised, as if it had
        # escaped the hook, so errno mapping and its one log line are what they always were.
        cause = e.failure.error if isinstance(e, HookFailed) else e
        if isinstance(e, HookFailed) and e.failure.timed_out:
            # Fail closed, as any other hook failure does; the traceback of a cancellation
            # says nothing, so one line naming the hook and the limit.
            log.error(
                "%s %s: %s [%s]",
                ctx.op,
                ctx.display_path,
                _timed_out(e.failure),
                ctx.correlation_id,
            )
            error = FsError(errno.EIO)
        else:
            error = _as_fs_error(ctx, cause, stage)

        async def notify(mw: Middleware, c: Context) -> None:
            report.ctx = c
            await mw.failed(c, error)

        pairs = None if observers is None else list(reversed(observers))
        await run.observe_ran("failed", notify, pairs=pairs)
        raise error from cause
    finally:
        if report.metrics is not None:
            report.metrics.hooks(run.timings)
    return result
