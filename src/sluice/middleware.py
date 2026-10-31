"""Middleware and the pipeline that runs it.

One call: `param()` callbacks → `pre` hooks (chain order) → the operation → `post` hooks
(reverse order). `failed` hooks observe any failure. Every hook is `async`: one event loop serves
the whole mount, so a hook that blocks stalls every call.
"""

from __future__ import annotations

import errno
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sluice.errors import Deny, FsError, RouteError

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


def _as_fs_error(ctx: Context, e: Exception, stage: str) -> FsError:
    if isinstance(e, FsError):
        return e
    if stage == "operation" and isinstance(e, OSError) and e.errno:
        return FsError(e.errno, e.strerror or "")
    log.error(
        "%s %s: unexpected error in %s [%s]",
        ctx.op,
        ctx.display_path,
        stage,
        ctx.correlation_id,
        exc_info=e,
    )
    return FsError(errno.EIO)


async def _failed(ran: Sequence[tuple[Middleware, Context]], error: FsError) -> None:
    for mw, ctx in reversed(ran):
        try:
            await mw.failed(ctx, error)
        except Exception:
            log.exception("%r.failed raised [%s]; ignored", mw, ctx.correlation_id)


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
    middleware on both chains runs once per path, so `auth()` checks the target as well.
    A `ctx.respond()` on either side stops the remaining `pre` hooks and skips the operation.
    Every hook that ran `pre` gets `post` or `failed` with the same context.
    """
    chain = [mw for mw in ctx.resolution.middlewares if mw.applies(ctx.op)]
    ran: list[tuple[Middleware, Context]] = []
    # Who `failed` reaches. A param() failure comes before its chain's pre hooks; let the whole
    # chain observe it, as well as the hooks that already ran.
    observers: Sequence[tuple[Middleware, Context]] = [(mw, ctx) for mw in chain]
    stage = "param()"
    try:
        await run_params(ctx, ctx.resolution.param_specs)
        stage, observers = "pre", ran
        for mw in chain:
            ran.append((mw, ctx))
            await mw.pre(ctx)
            if ctx.responded:
                break
        responder = ctx if ctx.responded else None
        for other in also:
            if responder is not None:
                break
            other_chain = [mw for mw in other.resolution.middlewares if mw.applies(other.op)]
            stage, observers = "param()", [*ran, *((mw, other) for mw in other_chain)]
            await run_params(other, other.resolution.param_specs)
            stage, observers = "pre", ran
            for mw in other_chain:
                ran.append((mw, other))
                await mw.pre(other)
                if other.responded:
                    responder = other
                    break
        if responder is None:
            stage = "operation"
            result = await operation(ctx)
        else:
            result = responder.response
        stage = "post"
        for mw, mw_ctx in reversed(ran):
            result = await mw.post(mw_ctx, result)
    except Exception as e:
        error = _as_fs_error(ctx, e, stage)
        await _failed(observers, error)
        raise error from e
    return result


async def allow_all(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
    """The phase 1 `verify`: allow everything."""
    return True


Verify = Callable[[Any, Any, Path | None, str], Awaitable[bool]]


class Auth(Middleware):
    """Deny a call unless `verify(user, token, real_path, op)` returns True."""

    def __init__(self, verify: Verify, user_param: str, token_param: str) -> None:
        self.verify = verify
        self.user_param = user_param
        self.token_param = token_param

    async def pre(self, ctx: Context) -> None:
        user = ctx.params.get(self.user_param)
        token = ctx.params.get(self.token_param)
        if not await self.verify(user, token, ctx.real_path, ctx.op):
            raise Deny(f"{ctx.op} denied")

    def __repr__(self) -> str:
        return f"auth({getattr(self.verify, '__qualname__', self.verify)!s})"


def auth(
    verify: Verify = allow_all,
    *,
    user_param: str = "user",
    token_param: str = "token",  # noqa: S107 - the name of a route param, not a credential
) -> Auth:
    """Authorisation hook point. `verify` is injected; the default allows everything."""
    check_coroutine(verify, "auth(verify)")
    return Auth(verify, user_param, token_param)


class AccessLog(Middleware):
    """Log every call's outcome: successes from `post`, denials and errors from `failed`."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger = logger
        self.level = level

    async def pre(self, ctx: Context) -> None:
        ctx.state.setdefault("access_log.start", time.monotonic())

    def _log(self, ctx: Context, outcome: str) -> None:
        start = ctx.state.get("access_log.start", time.monotonic())
        ms = (time.monotonic() - start) * 1000
        caller = ctx.caller
        self.logger.log(
            self.level,
            "%s %s uid=%d pid=%d -> %s (%.1fms) [%s]",
            ctx.op,
            ctx.display_path,
            caller.uid,
            caller.pid,
            outcome,
            ms,
            ctx.correlation_id,
        )

    async def post(self, ctx: Context, result: Any) -> Any:
        self._log(ctx, "ok")
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        self._log(ctx, errno.errorcode.get(error.errno, str(error.errno)))

    def __repr__(self) -> str:
        return "access_log()"


def access_log(logger: logging.Logger | None = None, level: int = logging.INFO) -> AccessLog:
    """Log each call: op, path (secret segments masked), caller, outcome, time."""
    return AccessLog(logger or logging.getLogger("sluice.access"), level)
