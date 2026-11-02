"""Backend calls abandoned by a timeout, and the cap on them (design 0006).

A hung backend (a stale NFS mount) is simulated by a function that blocks on a
`threading.Event` until the test releases it.
"""

import asyncio
import errno
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sluice import Context, FsError, Middleware, Sluice
from sluice import backend as backend_module
from sluice.backend import Passthrough
from sluice.context import Opened
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE
from sluice.metrics import Metrics


class Hang:
    """A backend function that blocks its thread until `release()`."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.go = threading.Event()

    def __call__(self, *args: object) -> str:
        self.started.set()
        self.go.wait(10)
        return "late"

    def release(self) -> None:
        self.go.set()


@pytest.fixture
def hang() -> Iterator[Hang]:
    h = Hang()
    yield h
    h.release()  # Never leave a pool thread blocked past the test


def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.001)


async def abandon(io: Passthrough, hang: Hang) -> None:
    """Start a hung call, wait until its thread runs it, then cancel the await."""
    task = asyncio.create_task(io.run(hang))
    while not hang.started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_default_cap_is_half_the_pool() -> None:
    for threads, cap in ((8, 4), (3, 1), (1, 1)):
        io = Passthrough(threads)
        assert io.max_abandoned == cap
        io.shutdown()
    io = Passthrough(4, max_abandoned=4)
    assert io.max_abandoned == 4
    io.shutdown()


def test_a_cancelled_running_call_is_abandoned_until_it_finishes(hang: Hang) -> None:
    io = Passthrough(2, max_abandoned=1)
    try:
        asyncio.run(abandon(io, hang))
        assert io.abandoned == 1
        hang.release()
        until(lambda: io.abandoned == 0)
    finally:
        io.shutdown()


def test_a_cancelled_queued_call_is_not_abandoned(hang: Hang) -> None:
    io = Passthrough(1, max_abandoned=1)
    ran: list[str] = []

    async def scenario() -> None:
        busy = asyncio.create_task(io.run(hang))  # Holds the only thread, not cancelled
        while not hang.started.is_set():
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(io.run(ran.append, "queued"))
        await asyncio.sleep(0.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert io.abandoned == 0  # It never started: cancel() took it off the queue
        hang.release()
        assert await busy == "late"

    try:
        asyncio.run(scenario())
        assert ran == []
    finally:
        io.shutdown()


def test_at_the_cap_calls_fail_fast_with_eio_then_recover(
    hang: Hang, caplog: pytest.LogCaptureFixture
) -> None:
    metrics = Metrics()
    io = Passthrough(4, max_abandoned=2, metrics=metrics)
    second = Hang()

    async def refused() -> int:
        with pytest.raises(FsError) as e:
            await io.run(os.getpid)
        return e.value.errno

    try:
        asyncio.run(abandon(io, hang))
        assert asyncio.run(io.run(os.getpid)) == os.getpid()  # 1 of 2: still served
        asyncio.run(abandon(io, second))
        assert io.abandoned == 2
        assert asyncio.run(refused()) == errno.EIO
        assert asyncio.run(refused()) == errno.EIO
        # Logged once per spell at the cap, however many calls it refuses.
        assert caplog.text.count("abandoned after a timeout are still running") == 1
        text = metrics.render().decode()
        assert "sluice_backend_abandoned_total 2" in text
        assert "sluice_backend_abandoned 2" in text
        assert "sluice_backend_refused_total 2" in text

        hang.release()
        until(lambda: io.abandoned == 1)
        assert "accepting backend calls again" in caplog.text
        assert asyncio.run(io.run(os.getpid)) == os.getpid()
        assert "sluice_backend_abandoned 1" in metrics.render().decode()
    finally:
        second.release()
        io.shutdown()


def test_release_is_never_refused(hang: Hang, tmp_path: Path) -> None:
    # Refusing a close would leak the fd: release runs even at the cap.
    io = Passthrough(2, max_abandoned=1)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        asyncio.run(abandon(io, hang))
        asyncio.run(io.release(Opened(fd=fd)))
        with pytest.raises(OSError) as e:
            os.fstat(fd)
        assert e.value.errno == errno.EBADF
    finally:
        hang.release()
        io.shutdown()


def test_a_hook_timed_out_on_ctx_backend_abandons_the_call(
    hang: Hang, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the hook awaits a hung `ctx.backend` call and pipeline.timeout_ms fires."""
    (tmp_path / "alice").mkdir()
    monkeypatch.setattr(backend_module, "_lstat", hang)

    class Check(Middleware):
        ops = frozenset({"lookup"})

        async def pre(self, ctx: Context) -> None:
            if ctx.backend is not None:
                await ctx.backend.lstat(".")

    app = Sluice()
    app.route("/u/{user}", backend=f"{tmp_path}/{{user}}", middlewares=[Check()])
    io = Passthrough(2, max_abandoned=1)
    d = Dispatcher(app.freeze(timeout_ms=20), io)

    async def lookup() -> int:
        parent = (await d.lookup(ROOT_INODE, "u")).inode
        with pytest.raises(FsError) as e:
            await d.lookup(parent, "alice")
        return e.value.errno

    try:
        assert asyncio.run(lookup()) == errno.EIO  # timed out: fails closed
        assert io.abandoned == 1
        assert asyncio.run(lookup()) == errno.EIO  # at the cap: refused, not queued
        hang.release()
        until(lambda: io.abandoned == 0)
    finally:
        io.shutdown()
