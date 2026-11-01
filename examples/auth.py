"""Authorisation as middleware: deny a call unless an injected `verify` allows it.

An example, not part of the sluice package: copy it beside your app module and adapt it.
`verify` is the policy; the default allows everything, so this file gives you the hook point,
not a policy.

    from auth import auth

    async def verify(user, token, real_path, op):
        return token == "T0k"

    app.route("/fuse/{user}/{token}", backend="/ext4/{user}", middlewares=[auth(verify)])

On `rename` the hook runs once per path, so `verify` sees the target as well as the source.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sluice import Context, Deny, Middleware, RouteError


async def allow_all(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
    """The default `verify`: allow everything."""
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
    # A sync verify would block the event loop that serves the whole mount.
    if not inspect.iscoroutinefunction(verify):
        raise RouteError(f"auth(verify): {verify!r} must be an async function (async def)")
    return Auth(verify, user_param, token_param)
