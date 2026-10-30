"""`Sluice`, the registration API, and loading the app named by `app.module`."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from sluice.context import MountConfig
from sluice.errors import AppLoadError, RouteError
from sluice.routing import ParamFunc, ParamSpec, Route, RouteTable, compile_table
from sluice.virtual import VirtualHandler


class Sluice:
    """An app: routes, global middleware and `param()` callbacks.

    `freeze()` validates it against the mount-time config and returns the `RouteTable` that serves.
    """

    def __init__(self) -> None:
        self._root = Route("/")
        self._params: dict[str, ParamSpec] = {}

    def route(
        self,
        pattern: str,
        backend: str | None = None,
        virtual: bool = False,
        handler: VirtualHandler | None = None,
        middlewares: Sequence[object] = (),
        direct_io: bool = False,
    ) -> Route:
        """Add a route. Its own `.route()` adds children with patterns relative to it."""
        return self._root.route(
            pattern,
            backend=backend,
            virtual=virtual,
            handler=handler,
            middlewares=middlewares,
            direct_io=direct_io,
        )

    def use(self, *middlewares: object) -> None:
        """Global middleware: runs first, on every call."""
        self._root.middlewares.extend(middlewares)

    def param(self, name: str, func: ParamFunc | None = None, secret: bool = False) -> None:
        """`await func(ctx, raw)` replaces `ctx.params[name]` once per call, before middleware.

        `secret=True` masks the value in logs, `display_path` and `sluice resolve`, and bans the
        param from backend templates.
        """
        self._params[name] = ParamSpec(name, func, secret)

    @property
    def root(self) -> Route:
        return self._root

    def freeze(self, config: MountConfig | None = None) -> RouteTable:
        """Validate the route tree (raising `RouteError`) and freeze it."""
        return compile_table(self._root, list(self._params.values()), config or MountConfig())


def load_app(spec: str, directory: str = "") -> Sluice:
    """Import `module:attribute` (after putting `directory` first on `sys.path`)."""
    module_name, sep, attr = spec.partition(":")
    if not spec:
        raise AppLoadError("app.module is not set (module:attribute, e.g. myroutes:app)")
    if not sep or not module_name or not attr:
        raise AppLoadError(f"app.module: expected module:attribute, got {spec!r}")
    if directory and directory not in sys.path:
        sys.path.insert(0, directory)
    try:
        module = importlib.import_module(module_name)
    except RouteError:
        raise
    except Exception as e:
        # Any error while importing the user's module is theirs to fix: one line, not a traceback.
        raise AppLoadError(f"app.module: cannot import {module_name}: {e!r}") from e
    try:
        app = getattr(module, attr)
    except AttributeError:
        raise AppLoadError(f"app.module: {module_name} has no attribute {attr!r}") from None
    if not isinstance(app, Sluice):
        raise AppLoadError(f"app.module: {spec} is a {type(app).__name__}, not a Sluice app")
    return app
