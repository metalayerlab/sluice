"""Metrics (design 0005): what is counted, the labels it may use, and `/.sluice/metrics`."""

import asyncio
import errno
import os
import sys
from collections.abc import Awaitable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from sluice import Context, Deny, FsError, Middleware, MountConfig, RouteError, Sluice
from sluice.backend import Passthrough
from sluice.config import load_config
from sluice.dispatch import Dispatcher
from sluice.inodes import ROOT_INODE
from sluice.metrics import ERRNO_NAMES, UNKNOWN_ERRNO, Metrics, errno_name
from sluice.middleware import OPS
from sluice.mount import prepare


class Gate(Middleware):
    async def pre(self, ctx: Context) -> None:
        if ctx.params.get("user") == "mallory":
            raise Deny()


class Crash(Middleware):
    ops = frozenset({"read"})

    async def pre(self, ctx: Context) -> None:
        raise RuntimeError("bug")


class Odd(Middleware):
    ops = frozenset({"open"})

    async def pre(self, ctx: Context) -> None:
        raise FsError(99999, "not an errno")


class NoisyFailed(Middleware):
    async def failed(self, ctx: Context, error: FsError) -> None:
        raise RuntimeError("in failed")


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
    info = await d.open(await walk(d, path), os.O_RDONLY)
    data = b""
    while chunk := await d.read(info.fh, len(data), 65536):
        data += chunk
    await d.release(info.fh)
    return data


def fails(coro: Awaitable[Any]) -> int:
    with pytest.raises(FsError) as e:
        run(coro)
    return e.value.errno


def app(backing: Path) -> Sluice:
    a = Sluice()
    a.use(Gate())
    user = a.route("/home/{user}", backend=f"{backing}/{{user}}")
    user.route("crash", middlewares=[Crash()])
    user.route("odd", middlewares=[Odd()])
    user.route("noisy", middlewares=[NoisyFailed()])
    return a


@pytest.fixture
def backing(tmp_path: Path) -> Path:
    root = tmp_path / "ext4"
    for d in ("alice/crash", "alice/odd", "alice/noisy"):
        (root / d).mkdir(parents=True)
    (root / "alice" / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "alice" / "crash" / "f").write_text("f", encoding="utf-8")
    (root / "alice" / "odd" / "f").write_text("f", encoding="utf-8")
    return root


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def d(backing: Path, metrics: Metrics) -> Iterator[Dispatcher]:
    io = Passthrough(2)
    yield Dispatcher(app(backing).freeze(MountConfig(), metrics=metrics), io)
    io.shutdown()


def sample(text: str, line: str) -> float:
    """The value of the sample whose name and labels are `line`."""
    for row in text.splitlines():
        name, _, value = row.rpartition(" ")
        if name == line:
            return float(value)
    raise AssertionError(f"no sample {line!r} in:\n{text}")


def test_an_empty_registry_declares_every_metric(metrics: Metrics) -> None:
    text = metrics.render().decode()
    for name, kind in [
        ("sluice_call_duration_seconds", "histogram"),
        ("sluice_call_errors_total", "counter"),
        ("sluice_middleware_seconds_total", "counter"),
        ("sluice_middleware_failures_total", "counter"),
    ]:
        assert f"# TYPE {name} {kind}\n" in text
    assert "{" not in text  # no samples until something happens


def test_calls_are_counted_by_op_with_their_errno(d: Dispatcher, metrics: Metrics) -> None:
    assert run(cat(d, "/home/alice/notes.txt")) == b"hello"
    assert fails(walk(d, "/home/alice/missing")) == errno.ENOENT
    assert fails(walk(d, "/home/mallory")) == errno.EACCES
    assert fails(walk(d, "/elsewhere")) == errno.ENOENT  # routing: before any pipeline
    text = metrics.render().decode()
    # lookup: 3 for notes.txt, 3 for the missing name, 2 for mallory, 1 for elsewhere.
    assert sample(text, 'sluice_call_duration_seconds_count{op="lookup"}') == 9
    assert sample(text, 'sluice_call_duration_seconds_count{op="open"}') == 1
    assert sample(text, 'sluice_call_duration_seconds_count{op="read"}') == 2  # data, then EOF
    assert sample(text, 'sluice_call_errors_total{op="lookup",errno="ENOENT"}') == 2
    assert sample(text, 'sluice_call_errors_total{op="lookup",errno="EACCES"}') == 1
    assert 'errno="' not in "".join(r for r in text.splitlines() if 'op="read"' in r)
    assert sample(text, 'sluice_call_duration_seconds_bucket{op="lookup",le="+Inf"}') == 9
    assert sample(text, 'sluice_call_duration_seconds_sum{op="lookup"}') > 0


def test_a_deny_is_an_error_not_a_middleware_failure(d: Dispatcher, metrics: Metrics) -> None:
    assert fails(walk(d, "/home/mallory")) == errno.EACCES
    assert "sluice_middleware_failures_total{" not in metrics.render().decode()


def test_middleware_failures_and_time_by_middleware(d: Dispatcher, metrics: Metrics) -> None:
    async def read_crash() -> bytes:
        return await cat(d, "/home/alice/crash/f")

    assert fails(read_crash()) == errno.EIO

    async def noisy() -> int:
        return await walk(d, "/home/alice/noisy/missing")

    assert fails(noisy()) == errno.ENOENT
    text = metrics.render().decode()
    assert sample(text, 'sluice_middleware_failures_total{middleware="Crash",hook="pre"}') == 1
    assert sample(text, 'sluice_call_errors_total{op="read",errno="EIO"}') == 1
    assert (
        sample(text, 'sluice_middleware_failures_total{middleware="NoisyFailed",hook="failed"}')
        == 1
    )
    assert sample(text, 'sluice_middleware_seconds_total{middleware="Gate"}') > 0


def test_an_errno_outside_the_kernels_table_is_unknown(d: Dispatcher, metrics: Metrics) -> None:
    async def open_odd() -> object:
        return await d.open(await walk(d, "/home/alice/odd/f"), os.O_RDONLY)

    assert fails(open_odd()) == 99999
    text = metrics.render().decode()
    assert sample(text, f'sluice_call_errors_total{{op="open",errno="{UNKNOWN_ERRNO}"}}') == 1
    assert errno_name(errno.ENOENT) == "ENOENT"
    assert errno_name(99999) == UNKNOWN_ERRNO
    assert UNKNOWN_ERRNO in ERRNO_NAMES


def test_an_unexpected_error_counts_as_eio(
    d: Dispatcher, metrics: Metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: PurePosixPath) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(d, "_resolve", broken)
    with pytest.raises(RuntimeError):
        run(d.lookup(ROOT_INODE, "home"))  # not an FsError: fs.py replies EIO
    text = metrics.render().decode()
    assert sample(text, 'sluice_call_errors_total{op="lookup",errno="EIO"}') == 1


def test_no_path_param_or_user_is_ever_a_label(d: Dispatcher, metrics: Metrics) -> None:
    run(cat(d, "/home/alice/notes.txt"))
    fails(walk(d, "/home/mallory"))
    text = metrics.render().decode()
    for value in ("alice", "mallory", "notes", "home", "/"):
        assert f'"{value}' not in text


def test_every_op_label_is_declared(metrics: Metrics) -> None:
    for op in sorted(OPS):
        metrics.call(op, 0.001, errno.EIO)
    with pytest.raises(ValueError, match="op"):
        metrics.call("forget", 0.001, None)


def test_the_metrics_file_is_read_only_and_rendered_at_open(d: Dispatcher) -> None:
    first = run(cat(d, "/.sluice/metrics")).decode()
    assert "# TYPE sluice_call_duration_seconds histogram" in first
    second = run(cat(d, "/.sluice/metrics")).decode()
    # The first read's own calls (its lookups, open, reads) show up in the next open's snapshot.
    assert sample(second, 'sluice_call_duration_seconds_count{op="open"}') == 1

    async def write() -> object:
        return await d.open(await walk(d, "/.sluice/metrics"), os.O_WRONLY)

    assert fails(write()) == errno.EROFS
    assert fails(walk(d, "/.sluice/other")) == errno.ENOENT


def test_sluice_dir_is_listed_and_lists_metrics(d: Dispatcher) -> None:
    async def ls(path: str) -> list[str]:
        fh = await d.opendir(await walk(d, path))
        names = [e.name for e in await d.readdir(fh)]
        await d.releasedir(fh)
        return names

    assert ".sluice" in run(ls("/"))
    assert run(ls("/.sluice")) == ["metrics"]


def test_global_middleware_gates_the_metrics_file(tmp_path: Path) -> None:
    class Closed(Middleware):
        async def pre(self, ctx: Context) -> None:
            raise Deny()

    a = Sluice()
    a.use(Closed())
    io = Passthrough(1)
    try:
        d = Dispatcher(a.freeze(metrics=Metrics()), io)
        assert fails(walk(d, "/.sluice/metrics")) == errno.EACCES
    finally:
        io.shutdown()


def test_sluice_dir_wins_over_a_root_param_route(tmp_path: Path) -> None:
    a = Sluice()
    a.route("/{user}", backend=f"{tmp_path}/{{user}}")
    table = a.freeze(metrics=Metrics())
    res = table.resolve("/.sluice")
    assert res is not None and res.kind == "synthetic"
    leaf = table.resolve("/.sluice/metrics")
    assert leaf is not None and leaf.kind == "virtual"


@pytest.mark.parametrize("pattern", ["/.sluice", "/.sluice/metrics", "/.sluice/{x}"])
def test_sluice_namespace_is_reserved_when_metrics_are_on(pattern: str) -> None:
    a = Sluice()
    a.route(pattern, backend="/srv")
    with pytest.raises(RouteError, match=r"/\.sluice is reserved"):
        a.freeze(metrics=Metrics())
    assert a.freeze().resolve(pattern.replace("{x}", "y")) is not None  # free with metrics off


def test_freezing_again_does_not_find_the_metrics_route() -> None:
    a = Sluice()
    a.freeze(metrics=Metrics())
    assert a.root.children == []
    a.freeze(metrics=Metrics())  # no "reserved" or "same shape" error
    assert a.freeze().resolve("/.sluice") is None


def test_without_metrics_nothing_is_routed_or_counted(backing: Path) -> None:
    table = app(backing).freeze()
    assert table.metrics is None
    assert table.resolve("/.sluice/metrics") is None
    res = table.resolve("/home/alice/notes.txt")
    assert res is not None and res.metrics is None


def test_every_middleware_name_is_declared(backing: Path, metrics: Metrics) -> None:
    app(backing).freeze(metrics=metrics)
    assert {"Gate", "Crash", "Odd", "NoisyFailed"} <= metrics.middleware.values


def test_prepare_turns_metrics_on_from_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "metricsapp.py").write_text(
        "from sluice import Sluice\napp = Sluice()\napp.route('/x', backend='/srv')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "metricsapp", raising=False)
    base = {"SLUICE_APP_MODULE": "metricsapp:app", "SLUICE_SECRETS_DIR": str(tmp_path / "no")}
    off = prepare(load_config(tmp_path / "n.yaml", environ=base))
    assert off.metrics is None
    on = prepare(load_config(tmp_path / "n.yaml", environ=base | {"SLUICE_METRICS_ENABLED": "1"}))
    assert on.metrics is not None
    assert on.resolve("/.sluice/metrics") is not None
