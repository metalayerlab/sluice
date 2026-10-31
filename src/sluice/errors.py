"""Exception types.

Configuration, app, route and mount errors stop sluice before it serves anything: the CLI prints
them as one line and exits 2. `FsError` and `Deny` are raised while serving, to fail one call with
an errno; the mount carries on.
"""

from __future__ import annotations

import errno as _errno
import os

from sluice.config import ConfigError

__all__ = ["AppLoadError", "ConfigError", "Deny", "FsError", "MountError", "RouteError"]


class AppLoadError(Exception):
    """`app.module` is empty, malformed, not importable, or not a `Sluice` app."""


class RouteError(Exception):
    """The route tree is invalid. The message names the route pattern."""


class MountError(Exception):
    """The filesystem can't be mounted."""


class FsError(Exception):
    """Fail one filesystem call with `errno`."""

    def __init__(self, errno: int, msg: str = "") -> None:
        self.errno = errno
        self.msg = msg or os.strerror(errno)
        super().__init__(errno, self.msg)

    def __str__(self) -> str:
        name = _errno.errorcode.get(self.errno, str(self.errno))
        return f"{name}: {self.msg}"


class Deny(FsError):
    """Refuse a call: `EACCES` to the caller."""

    def __init__(self, msg: str = "denied") -> None:
        super().__init__(_errno.EACCES, msg)
