"""The examples are copied beside an app, not installed: import them from `examples/`."""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from sluice import FsError, Sluice
from sluice.backend import Passthrough
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE

EXAMPLES = Path(__file__).parents[2] / "examples"
sys.path.insert(0, str(EXAMPLES))


@pytest.fixture
def mount() -> Iterator[Callable[[Sluice], Dispatcher]]:
    """A `Dispatcher` over an app: everything but the kernel."""
    pools: list[Passthrough] = []

    def make(app: Sluice) -> Dispatcher:
        pools.append(Passthrough(2))
        return Dispatcher(app.freeze(), pools[-1])

    yield make
    for io in pools:
        io.shutdown()


def run[T](coro: Awaitable[T]) -> T:
    async def wrap() -> T:
        return await coro

    return asyncio.run(wrap())


async def walk(d: Dispatcher, path: str) -> int:
    inode = ROOT_INODE
    for name in PurePosixPath(path).parts[1:]:
        inode = (await d.lookup(inode, name)).inode
    return inode


async def cat(d: Dispatcher, path: str) -> bytes:
    info = await d.open(await walk(d, path), 0)
    data = b""
    while chunk := await d.read(info.fh, len(data), 1 << 16):
        data += chunk
    await d.release(info.fh)
    return data


async def ls(d: Dispatcher, path: str) -> dict[str, Any]:
    """Names to attributes, as `ls -l` sees them."""
    fh = await d.opendir(await walk(d, path))
    entries = {e.name: e.attr for e in await d.readdir(fh)}
    await d.releasedir(fh)
    return entries


def errno_of(coro: Awaitable[Any]) -> int:
    with pytest.raises(FsError) as e:
        run(coro)
    return e.value.errno


def vo_checkout() -> Path | None:
    """The Virtual Organisations checkout the VO adapter's integration tests read, if usable.

    `VO_CHECKOUT` names it, and its model package must be importable (design 0011, question 1).
    """
    from importlib.util import find_spec

    root = os.environ.get("VO_CHECKOUT")
    if not root or find_spec("virtual_organisations") is None:
        return None
    return Path(root)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect, not skip, the VO adapter's integration tests without a checkout and the package.

    The model is private, so CI can't install it, and CI's integration job fails on a skip.
    """
    if vo_checkout() is not None:
        return
    name = "test_vo_projection.py"
    vo = [i for i in items if i.path.name == name and "integration" in i.keywords]
    if vo:
        config.hook.pytest_deselected(items=vo)
        items[:] = [i for i in items if i not in vo]
