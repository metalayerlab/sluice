"""sluice's metrics (design 0005), served as the read-only virtual file `/.sluice/metrics`.

In memory only: counting a call is a counter increment and a histogram bucket, never I/O. The
file's content is rendered once per `open`, as every virtual file's is. Every label takes its
values from a set declared up front (interpose refuses any other): operations from `OPS`, errno
names from the kernel's table, hooks from the three sluice has, and middleware names from the
frozen route tree. A path, a route param or a caller is never a label: per-path detail is what an
access-log middleware is for.
"""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING

from interpose.metrics import CONTENT_TYPE, Counter, Histogram, Label, Registry

from sluice.middleware import OPS
from sluice.virtual import VirtualHandler

if TYPE_CHECKING:
    from collections.abc import Iterable

    from interpose.pipeline import Failure

    from sluice.context import Context

__all__ = ["CONTENT_TYPE", "DURATION_BUCKETS", "PATH", "Metrics", "MetricsFile"]

PATH = "/.sluice/metrics"
NAMESPACE = ".sluice"  # The root name sluice's own files live under (design 0005)

# Seconds. Fixed: a bucket layout is a dashboard contract. From a cached-metadata call (tens of
# microseconds) to a slow backend or network-backed middleware (seconds).
DURATION_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)

UNKNOWN_ERRNO = "unknown"  # An FsError whose number isn't in the kernel's errno table
ERRNO_NAMES = frozenset(errno.errorcode.values()) | {UNKNOWN_ERRNO}
HOOKS = ("pre", "post", "failed")


def errno_name(code: int) -> str:
    return errno.errorcode.get(code, UNKNOWN_ERRNO)


class Metrics:
    """The mount's metrics. One per mount, built before the route tree is frozen."""

    def __init__(self) -> None:
        op = Label("op", OPS)
        self.middleware = Label("middleware")  # Declared from the frozen route tree
        self.call_duration = Histogram(
            "sluice_call_duration_seconds",
            "Filesystem calls through the dispatcher, by operation: count and duration,"
            " failed calls included.",
            DURATION_BUCKETS,
            [op],
        )
        self.call_errors = Counter(
            "sluice_call_errors_total",
            "Filesystem calls that failed, by operation and the errno returned to the kernel.",
            [op, Label("errno", ERRNO_NAMES)],
        )
        self.middleware_seconds = Counter(
            "sluice_middleware_seconds_total",
            "Time spent in each middleware's hooks, in seconds.",
            [self.middleware],
        )
        self.middleware_failures = Counter(
            "sluice_middleware_failures_total",
            "Middleware hooks that failed, by middleware and hook: a pre or post hook that"
            " raised something other than an FsError (a deliberate answer), or a failed hook"
            " that raised anything.",
            [self.middleware, Label("hook", HOOKS)],
        )
        self.registry = Registry(
            [
                self.call_duration,
                self.call_errors,
                self.middleware_seconds,
                self.middleware_failures,
            ]
        )

    def declare_middleware(self, names: Iterable[str]) -> None:
        self.middleware.declare(*names)

    def call(self, op: str, seconds: float, code: int | None) -> None:
        """One call finished: `code` is the errno it failed with, or None."""
        self.call_duration.observe(seconds, op)
        if code is not None:
            self.call_errors.inc(op, errno_name(code))

    def hooks(self, timings: dict[str, float]) -> None:
        """One call's time in each middleware (interpose's `Run.timings`)."""
        for name, seconds in timings.items():
            self.middleware_seconds.inc(name, amount=seconds)

    def failure(self, failure: Failure) -> None:
        self.middleware_failures.inc(failure.middleware, failure.hook)

    def render(self) -> bytes:
        return self.registry.render().encode()


class MetricsFile(VirtualHandler):
    """`/.sluice/metrics`: the Prometheus text, rendered when the file is opened."""

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics

    async def render(self, ctx: Context) -> bytes:
        return self.metrics.render()
