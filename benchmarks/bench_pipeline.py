"""Benchmark `run_pipeline` with and without `skip_inherited` (designs 0008, 0009).

A sluice call's middleware cost, in µs per call, for two five-middleware chains with no-op
hook bodies and a no-op operation, so the numbers are the pipeline's own cost:

- **typical**: an auth gate (`pre` only), an access log (`pre`, `post`, `failed`), a hide rule
  and a transform (`pre`, `post`), and a plain `async def` gate: 2 of the 10 hooks on a
  successful call are the base class's no-ops;
- **gates**: five `pre`-only gates: 5 of the 10 are no-ops.

Each runs against the route tree frozen with sluice's `POLICY` (no `skip_inherited`) and with
`skip_inherited=Middleware` added. Numbers vary by machine and run; compare within one run.
Not a test: nothing asserts timings.

    uv run benchmarks/bench_pipeline.py [--n 100000]
"""

import argparse
import asyncio
import dataclasses
import sys
import time
from typing import Any

from sluice import Context, FsError, Middleware, MountConfig, Sluice
from sluice import middleware as mw_module
from sluice.middleware import run_pipeline
from sluice.routing import RouteTable


class Gate(Middleware):
    async def pre(self, ctx: Context) -> None:
        return None


class Log(Middleware):
    async def pre(self, ctx: Context) -> None:
        return None

    async def post(self, ctx: Context, result: Any) -> Any:
        return result

    async def failed(self, ctx: Context, error: FsError) -> None:
        return None


class Rewrite(Middleware):
    async def pre(self, ctx: Context) -> None:
        return None

    async def post(self, ctx: Context, result: Any) -> Any:
        return result


async def gate(ctx: Context) -> None:
    return None


CHAINS: dict[str, list[object]] = {
    "typical": [Gate(), Log(), Rewrite(), Rewrite(), gate],
    "gates": [Gate(), Gate(), Gate(), Gate(), gate],
}


def freeze(chain: list[object], skip: bool) -> RouteTable:
    """The tree frozen with sluice's POLICY, or with skip_inherited=Middleware added."""
    saved = mw_module.POLICY
    mw_module.POLICY = dataclasses.replace(saved, skip_inherited=Middleware if skip else None)
    try:
        app = Sluice()
        app.route("/t/{user}", backend="/srv/{user}", middlewares=chain)
        return app.freeze()
    finally:
        mw_module.POLICY = saved


async def operation(ctx: Context) -> bytes:
    return b""


async def per_call(table: RouteTable, n: int) -> float:
    res = table.resolve("/t/alice/f")
    if res is None:
        raise SystemExit("the benchmark route didn't resolve")

    async def one() -> None:
        ctx = Context("read", res, config=MountConfig(), correlation_id="b")
        await run_pipeline(ctx, operation)

    for _ in range(min(n, 2000)):  # warm up
        await one()
    start = time.perf_counter()
    for _ in range(n):
        await one()
    return (time.perf_counter() - start) / n * 1e6


async def main(n: int) -> None:
    print(f"Python {sys.version.split()[0]}, n={n}, µs per call through run_pipeline")
    for name, chain in CHAINS.items():
        # Alternate, best of three, so drift in the machine's load hits both sides alike.
        off, on = [], []
        for _ in range(3):
            off.append(await per_call(freeze(chain, skip=False), n))
            on.append(await per_call(freeze(chain, skip=True), n))
        print(f"{name:8s}  every hook awaited {min(off):6.2f}   skip_inherited {min(on):6.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=100_000, help="calls per measurement")
    asyncio.run(main(parser.parse_args().n))
