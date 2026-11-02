# 0006: Pipeline timeout and abandoned backend calls

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved by Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

## Context

A middleware hook that never returns (a network call to a dead host, a backend read on a hung NFS
mount) holds its FUSE request forever: the caller blocks in the kernel, uninterruptibly for many
operations, and nothing in sluice ends it. Design 0004 left the hook timeout to its own record (open
question 1): interpose's guard already bounds each hook call with `asyncio.timeout` when its
`Policy.timeout` is set, so sluice only has to choose the setting, the errno and what happens to the
work a timeout interrupts.

That last part is the catch. A timeout **cancels** the hook: `CancelledError` is raised at the `await`
it is suspended in. Work the hook started in a thread is not cancelled with it. A hook's blocking work
goes through `ctx.backend`, which runs it in the I/O pool (`io.threads`, `Passthrough.run`); a syscall
already running there keeps its thread until it returns. On a hung mount each timeout would leave one
more stuck thread, until the whole pool is stuck and every call (backed operations included) queues
forever: the timeout would turn one hung hook into a hung mount, more slowly.

Constraints:

- **Off by default.** A call that is slow today must not start failing on upgrade.
- **Fail closed.** A hook that didn't finish didn't approve the call.
- Settings are integers (interpose has no float setting type), with `check=`; a rule across settings
  goes in `load_config` (design 0003).
- Nothing per call beyond what the guard already does (design 0004: it already enters an
  `asyncio.timeout(None)` per hook call).

## Decision

### `pipeline.timeout_ms`

| Key | Variable | Type | Default |
|---|---|---|---|
| `pipeline.timeout_ms` | `SLUICE_PIPELINE_TIMEOUT_MS` | int, `check=non_negative` | `0` (off) |

- `0` is off, as for `pipeline.slow_ms` and `mount.cache_ms` (hence `non_negative`, not `positive`).
  Above 0 it is interpose's `Policy.timeout`, in seconds (`timeout_ms / 1000`), for **every hook call**:
  each `pre`, `post` and `failed` call separately, not the call as a whole.
- `middleware.policy(timeout_ms)` returns `POLICY` with the timeout (`POLICY` itself when 0);
  `compile_table` builds every route's `Pipeline` with it. `Sluice.freeze()` gains a keyword-only
  `timeout_ms=0`, and `prepare()` passes the setting. `POLICY` stays the no-timeout constant.
- **A timed-out `pre` or `post` fails the call with `EIO`**, as any other hook failure does, and the
  `failed` hooks of everything that ran are called, the timed-out one included. `EIO` rather than
  `ETIMEDOUT`: callers and tools treat `EIO` as "the filesystem failed", and a hook failure already
  means `EIO`; the cause is in the log. A `post` timeout fails a call whose operation already happened
  (a write that was written): the same as a `post` that raises today.
- **A timed-out `failed` hook** is ignored, like a `failed` hook that raises: the outcome is fixed.
- **Logging.** One line, no traceback (the cancellation's says nothing): `<op> <path>: <middleware>.<hook>
  timed out after <s>s [<correlation id>]`, or `... ; ignored` for a `failed` hook. A `TimeoutError` the
  hook raises itself (its own client's timeout) is not the guard's and is an ordinary failure, as before.
  `sluice_middleware_failures_total` counts timeouts with the other hook failures.
- Not bounded: `param()` callbacks, the operation itself and virtual handlers (they run outside the
  guard), and the kernel's own request time.
- No per-middleware timeout: entries are still built without one (design 0004). interpose supports
  it (`Entry.timeout`, `NO_LIMIT`); exposing it is a later, separate change.

### Hooks must be cancel-safe

With a timeout on, any hook can be cancelled at any `await`. The middleware contract, in the wiki and in
`middleware.py`:

- a hook must leave shared state consistent at every `await` (use `try`/`finally` or `async with`);
- it must **never swallow `CancelledError`** (no bare `except BaseException`, re-raise after cleanup);
  a hook that does keeps running past its limit and the call is not failed;
- blocking work goes through `ctx.backend` (the I/O pool), never on the event loop: a hook that blocks
  the loop can't be cancelled at all, and stalls every call.

### Abandoned backend calls, and a cap on them

`Passthrough.run` submits each call to the pool itself and, when its `await` is cancelled:

- a call still **queued** is taken off the queue (`Future.cancel()`): it never runs;
- a call already **running** is *abandoned*: counted until its thread returns (a done-callback on the
  future decrements the count, under a lock, from the worker thread).

| Key | Variable | Type | Default |
|---|---|---|---|
| `backend.max_abandoned` | `SLUICE_BACKEND_MAX_ABANDONED` | int, `check=non_negative`, at most `io.threads` | `0`: half of `io.threads`, at least 1 |

While `max_abandoned` calls are abandoned, **every new backend call fails at once with `EIO`** instead
of queueing: the operation of a backed route, and `ctx.backend` in hooks. The first refusal of a spell
is logged once (`N backend calls abandoned after a timeout are still running (backend.max_abandoned=M):
failing backend calls with EIO until they finish`); when the count drops below the cap, one line says
calls are accepted again, and they are. `release` (closing an fd) is never refused: refusing it would
leak the descriptor.

The default, half the pool (4 of the default 8), keeps half the threads for calls to backends that are
still healthy while a hung one accumulates abandoned calls. A value above `io.threads` is a
`ConfigError` (`load_config`): the pool would be full before the cap was reached. With the timeout off,
nothing is ever abandoned and the cap never applies.

Metrics (design 0005, with `metrics.enabled`):

| Metric | Type | What |
|---|---|---|
| `sluice_backend_abandoned_total` | counter | Backend calls abandoned after a cancel |
| `sluice_backend_abandoned` | gauge | Abandoned calls still running, read when the file renders |
| `sluice_backend_refused_total` | counter | Calls refused with `EIO` at the cap |

### Why the cap is a prerequisite for the timeout

Without it, turning the timeout on against a hung backend converts each timed-out hook into a
permanently occupied pool thread. After `io.threads` timeouts the pool is full, and from then on every
backend call, from hooks and operations alike and for every route, queues behind threads that will
never return: the mount hangs exactly as it would have without a timeout, only later, and the
timeouts keep firing on hooks stuck in the queue rather than on the hung syscall. The cap bounds that:
at most `max_abandoned` threads are lost, the rest of the pool keeps serving, and calls that would only
queue fail fast with `EIO`, which a caller can see and retry. Both land together, so there is no
release in which the timeout can be turned on without the cap.

## Alternatives considered

- **`ETIMEDOUT` for a timeout.** Names the cause, but no filesystem caller expects it from `open` or
  `read`, and it would differ from every other hook failure. The log line names the cause.
- **A timeout for the whole call** (hooks plus operation). The operation's syscall can't be cancelled
  either, and a whole-call budget makes one slow hook eat the next one's time. Per hook is what
  interpose's guard does.
- **`positive` with a separate `pipeline.timeout_enabled` flag.** Two settings for one; `0` is off for
  every other millisecond setting.
- **Kill or interrupt the stuck thread.** Python can't stop a thread in a syscall; signalling it
  (`pthread_kill`) leaves the syscall to restart, or the process state unknown.
- **Grow the pool as threads are abandoned.** Unbounded threads on a hung mount, each holding a stuck
  syscall: the same failure, with memory.
- **No cap, only a metric.** Leaves the hang above in place; an operator would have to restart the mount.
- **Queue at the cap instead of failing.** Queued calls wait for threads that may never return: the hang
  again.
- **Default cap equal to `io.threads`.** Refuses only once every thread is stuck, when healthy backends
  already have no thread left.

## Consequences

- Off by default: no change for existing mounts. With the timeout on, a hung hook fails its call with
  `EIO` after the limit instead of hanging it, and a hung backend costs at most `max_abandoned` threads.
- Middleware authors must write cancel-safe hooks once timeouts may be on; the wiki says so.
- A timed-out backend call still finishes in its thread; its result is dropped. A write that times out
  in a hook may still have been written.
- At the cap, backed calls on every route fail with `EIO` until the abandoned calls return: one hung
  backend can make healthy backends fail fast for a while, instead of hanging them for good.
- Unmounting waits for abandoned calls (`ThreadPoolExecutor.shutdown(wait=True)`), as it waited for any
  running call before.
- New public interface: two settings, `Sluice.freeze(timeout_ms=)`, `Passthrough(max_abandoned=,
  metrics=)` and `Passthrough.abandoned`, three metrics.
- Rollback: set `pipeline.timeout_ms` to 0, or revert; nothing persists.
- Wiki: In-Depth-Configuration (both settings, generated table), Writing-an-App (cancel-safe hooks),
  Architecture-Internals, Troubleshooting (the two log lines), Testing-Guide (the tests), FAQ;
  `contrib/sluice.example.yaml`.

## Open questions

None: approved by Stephen on 2026-11-02 (timeout off by default, `EIO`, the cap prerequisite).
