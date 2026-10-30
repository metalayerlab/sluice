"""Sluice — a Python FUSE filesystem with an Express-style router.

It gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and
their backing stores. The public API is re-exported here: build a `Sluice` app, add routes,
middleware and virtual handlers, and serve it with `sluice mount`.
"""

from importlib.metadata import PackageNotFoundError, version

from sluice.app import Sluice
from sluice.context import Attr, Caller, Context, DirEntry, MountConfig, Opened, Secret
from sluice.errors import Deny, FsError, RouteError
from sluice.middleware import Middleware, access_log, auth
from sluice.routing import Route
from sluice.virtual import VirtualHandler

try:
    __version__ = version("sluice")
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = "0+unknown"

__all__ = [
    "Attr",
    "Caller",
    "Context",
    "Deny",
    "DirEntry",
    "FsError",
    "Middleware",
    "MountConfig",
    "Opened",
    "Route",
    "RouteError",
    "Secret",
    "Sluice",
    "VirtualHandler",
    "__version__",
    "access_log",
    "auth",
]
