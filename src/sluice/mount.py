"""Mount lifecycle: load the app and mount-time config, freeze the routes, mount, serve, unmount.

pyfuse3 is imported here lazily, so everything else works (and is tested) without libfuse.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import cast

from sluice.app import load_app
from sluice.backend import Passthrough
from sluice.config import Config
from sluice.context import load_mount_config
from sluice.dispatch import Dispatcher
from sluice.errors import MountError
from sluice.routing import RouteTable

log = logging.getLogger(__name__)

FUSE_DEVICE = Path("/dev/fuse")


def prepare(cfg: Config) -> RouteTable:
    """Import the app, load `MountConfig` and freeze the routes: what every command needs."""
    app = load_app(cfg.app_module, cfg.app_dir)
    mount_config = load_mount_config(cfg)
    table = app.freeze(mount_config)
    if mount_config.secret:
        log.info("secrets loaded: %s", ", ".join(sorted(mount_config.secret)))
    return table


def check_mountpoint(point: str, device: Path = FUSE_DEVICE) -> Path:
    if not point:
        raise MountError("mount.point is not set")
    path = Path(point)
    if not path.is_dir():
        raise MountError(f"mount.point: {path} is not an existing directory")
    try:
        if any(path.iterdir()):
            raise MountError(f"mount.point: {path} is not empty")
    except OSError as e:
        hint = " (a stale mount? try fusermount3 -u)" if e.errno == errno.ENOTCONN else ""
        raise MountError(f"mount.point: cannot read {path}: {e.strerror}{hint}") from e
    if not device.exists():
        raise MountError(f"{device} not found: load the fuse module, or give the container it")
    return path


def fuse_options(cfg: Config, defaults: frozenset[str]) -> set[str]:
    options = set(defaults) | {f"fsname={cfg.mount_fsname}"}
    if cfg.mount_allow_other:
        options.add("allow_other")
    if cfg.mount_debug:
        options.add("debug")
    return options


def serve(cfg: Config) -> None:
    """Mount and serve in the foreground until SIGINT, SIGTERM or an unmount."""
    table = prepare(cfg)
    point = check_mountpoint(cfg.mount_point)
    try:
        import pyfuse3
        import pyfuse3.asyncio

        from sluice.fs import SluiceOperations
    except ImportError as e:
        raise MountError(f"pyfuse3 is not installed ({e}): install sluice[fuse]") from e

    io = Passthrough(cfg.io_threads)
    ops = SluiceOperations(Dispatcher(table, io), cfg.mount_cache_ms / 1000)
    pyfuse3.asyncio.enable()
    try:
        pyfuse3.init(
            cast("pyfuse3.Operations", ops),
            str(point),
            fuse_options(cfg, pyfuse3.default_options),
        )
    except RuntimeError as e:
        io.shutdown()
        raise MountError(f"cannot mount at {point}: {e} (libfuse's reason is logged above)") from e
    log.info("serving %s at %s (pid %d)", cfg.app_module, point, os.getpid())
    stopper = _Stopper(pyfuse3.terminate)
    # Ours, not the loop's, and never removed: Ctrl-C under `uv run` arrives twice (to the
    # process group, and forwarded). A second pyfuse3.terminate() raises in pyfuse3.asyncio, and
    # a signal after asyncio.run() had removed its handlers would kill the unmount or the exit.
    for sig in _STOP_SIGNALS:
        signal.signal(sig, stopper.handle)
    try:
        asyncio.run(_main(stopper, cfg.pipeline_slow_ms))
    finally:
        pyfuse3.close(unmount=True)
        io.shutdown()
        log.info("unmounted %s", point)


_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class _Stopper:
    """Ends the FUSE loop on the first stop signal; later ones are ignored."""

    def __init__(self, terminate: Callable[[], None]) -> None:
        self.terminate = terminate
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopping = False

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.stopping:
            return
        self.stopping = True
        log.info("%s: unmounting", signal.Signals(signum).name)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.terminate)

    def started(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        if self.stopping:  # A signal before the loop existed
            loop.call_soon(self.terminate)


async def _main(stopper: _Stopper, slow_ms: int) -> None:
    import pyfuse3

    loop = asyncio.get_running_loop()
    if slow_ms:
        # asyncio's debug mode logs any step that holds the loop longer than this.
        loop.slow_callback_duration = slow_ms / 1000
        loop.set_debug(True)
    stopper.started(loop)
    await pyfuse3.main()
