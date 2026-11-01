"""The examples are copied beside an app, not installed: import them from `examples/`."""

import asyncio
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
