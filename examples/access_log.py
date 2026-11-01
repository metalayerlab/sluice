"""An access log as middleware: one line per call, its outcome and its time.

An example, not part of the sluice package: copy it beside your app module and adapt it.

    from access_log import access_log

    app.use(access_log())  # every call, to the `sluice.access` logger

Paths are logged as `ctx.display_path`, so segments captured by `secret=True` params show as
`********`. Use it in your own logging middleware too.
"""

from __future__ import annotations

import errno
import logging
import time
from typing import Any

from sluice import Context, FsError, Middleware


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
