"""Sluice — a Python FUSE filesystem with an Express-style router.

It gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and
their backing stores.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sluice")
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
