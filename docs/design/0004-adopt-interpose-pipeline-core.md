# 0004: Adopt interpose pipeline core

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved under Stephen's overnight delegation; Stephen to confirm on review)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

## Context

`run_pipeline` (`middleware.py`) runs a call's middleware: `param()` callbacks, `pre` hooks in chain
order (any may deny or `ctx.respond()`), the operation, `post` hooks in reverse, and `failed` on every
hook that ran when anything fails. mitm-proxy runs the same loop around an HTTP flow. interpose's
[design 0003](https://github.com/metalayerlab/interpose/blob/v0.1.2/docs/design/0003-pipeline-core.md)
(v0.1.2) extracts that loop and the guard around each hook into `interpose.pipeline`, with each
project's semantics stated as a `Policy`, and briefs this adoption under "Consumer adoption".

Constraints:

- **No observable change.** The errno each failure becomes, which hooks run and in what order, and the
  log lines stay as they are. sluice stays fail-closed.
- `run_pipeline(ctx, operation, *, also=())` keeps its signature: `dispatch.py` and the tests call it.
- Nothing per call beyond the loop: no I/O, nothing built that could be built at freeze time
  (TODO.md, "Per-call env/secret resolution").
- No built-in middleware, no contrib, no `next()` middleware (TODO.md, "Decided against"; interpose's
  reconciliation table keeps all three).

## Decision

- **Dependency:** the pin moves to `interpose @ git+https://github.com/metalayerlab/interpose@v0.1.2`,
  still the first entry of `dependencies`, in PEP 508 form, never PyPI (design 0003). v0.1.1's metrics
  come with it and are unused here.
- **Policy**, a constant in `middleware.py`, is today's behaviour written down:

  ```python
  POLICY = Policy(
      on_error="fail",  # a failing hook fails the call
      after_order="reverse",  # post hooks: the onion
      after_local=True,  # post hooks also run after a ctx.respond()
      observe_failed=True,  # failed() reaches the hook that failed too
      answers=(FsError,),  # Deny / FsError is a hook's answer, not a failure
      skip_inherited=Middleware,  # a hook left as the base class's no-op isn't awaited
  )
  ```

  No `timeout` (see [Open for Stephen](#open-for-stephen)).
- **Frozen per route.** `compile_table` gives every route `Node` a `Pipeline` of the full chain from the
  root, global middleware first, and `Resolution.pipeline` is the matched node's. One `Entry` per
  registration, built once and shared with the descendants, so the same middleware object registered
  twice is two entries (it runs twice, as today). Nothing about the chain is built per call.
- **Names.** An entry is named by the middleware's `repr`: what `sluice routes`, `sluice resolve` and
  the logs already print (the class name, or a plain function's qualified name). interpose's default
  (`name` attribute, else class name) would rename middleware in the logs: the test suite's own
  `Recorder` has a `name` attribute that means something else. Entries are built explicitly, so a
  middleware's own `on_error` or `timeout` attribute is not read.
- **`run_pipeline`** starts a `Run` per call (`state=ctx.state`) and drives it:
  - selection by `ops` stays a list comprehension in sluice rather than `start(select=)`, so an
    `applies()` that raises escapes the call exactly as it does today;
  - `param()` callbacks stay sluice's (`run_params`), outside the guard;
  - pre: `run.before("pre", ctx, ..., until=ctx.responded)`, then, for `also` (rename's target), the
    same with the other context and its chain unless a hook already responded;
  - post: `run.after("post", result, ...)`.
- **Failure path.** The guard raises `HookFailed` for a hook that fails. sluice maps the hook's own
  exception (`failure.error`) through the existing `_as_fs_error`, with the stage the call was in, so an
  `OSError` keeps its errno, `_NOT_FS_ERRORS` still become `EIO`, and the one "unexpected error in
  `<stage>`" line is logged as before. Exceptions from `param()` and the operation take the same path
  without the wrapper. Then `run.observe_ran("failed", ...)` notifies the middleware that ran, in reverse,
  its own failure included; for a `param()` failure, the whole chain is passed as `pairs=` (reversed),
  as today. `FsError` from a hook is an answer: it passes through unwrapped and isn't logged.
- **One log line per failure.** `on_failure` sees every failure. A `"fail"` one ends the call and is
  logged by `_as_fs_error`, so `on_failure` logs only failures the pipeline carried on from: today those
  are `failed()` hooks that raised, logged as `<middleware>.failed raised [<correlation id>]; ignored`
  with the context of the hook that raised (rename's target has its own correlation ID).
- **`check_coroutine` stays.** `require_async` would change its message and exception type; sluice's
  `RouteError` line is part of the CLI's one-line errors.

## Alternatives considered

- **Keep sluice's loop.** Two copies of the same mechanism drift, and per-middleware failure policy and
  timeouts would have to be built twice.
- **interpose's `name_of` for names.** Renames middleware in the logs wherever a middleware has a `name`
  attribute or its own `repr`; `repr` is the name sluice already shows everywhere.
- **`start(select=lambda mw: mw.applies(op))`.** An `applies()` that raises would become a hook failure
  (`EIO` and a different log line) instead of escaping the call.
- **Log in `_as_fs_error` and in `on_failure` both.** The same failure twice. **Stop logging in the
  mapper** instead: `param()` and operation errors never reach `on_failure`, so they would need a second
  logging site anyway.
- **A default timeout now** (interpose's synergy note: fail closed with `ETIMEDOUT` or `EIO`). A new
  setting and a behaviour change: a call that is slow today would fail. It needs its own record.
- **Opt-in `on_error = "skip"` per middleware now** (interpose's brief, item 7). A new public attribute
  on `Middleware`; left for the same follow-up as the timeout, since both are per-middleware policy.

## Consequences

- One loop and one guard for sluice and mitm-proxy, tested in interpose. Per-middleware `on_error` and
  timeouts become a small, separate change.
- No observable change: the existing pipeline tests (`tests/unit/test_middleware.py`: every rule on
  every side of a two-path call, the logs) pass unmodified. New tests pin the policy, the per-route
  freezing, entry sharing, un-overridden hooks not being awaited, one log line per failure, and the
  `failed()` log naming its own context.
- **Cost.** The guard times and bounds each hook. Measured on the workstation (Python 3.13, five global
  middleware of which three override `pre` and two `post`, a no-op operation): about 22 µs per call,
  against 5 µs before. Most of it is interpose's guard (`asyncio.timeout`, two clock reads, and the
  `skip_inherited` check, recomputed per call). It is small next to a FUSE round trip with a worker-thread
  syscall, but not nothing; caching the `skip_inherited` lookup per class would be interpose's fix.
- The guard's per-middleware `Run.timings` are now available per call, unused here; design 0005
  (metrics) uses them.
- Rollback is reverting the change; nothing persistent depends on it.
- Wiki: Architecture-Internals (where the loop lives, the policy), Testing-Guide (the new tests).

## Open for Stephen

Each is implemented with the option that keeps today's behaviour; each is reversible.

1. **No timeout** (interpose 0003, decision 7). Recommended next: a record for a `pipeline.timeout_ms`
   setting (integer, `0` off, interpose's "no float setting type") and the errno a timeout becomes
   (`ETIMEDOUT` names the cause; `EIO` is what any other hook failure gives), plus a per-middleware
   `timeout`. Future work, not in this change.
2. **No per-middleware `on_error`** (decision 4's skip side, brief item 7). `FsError` already passes
   through as an answer (decision 4), so a later `on_error = "skip"` on, say, the access-log example
   would keep denials working. Recommended with the timeout record.
3. **Names are the `repr`, not interpose's `name_of`** (decision 9 is unaffected: names needn't be
   unique). Recommended, to keep the logs and `sluice routes` consistent.
4. **`skip_inherited=Middleware` is on** (decision 6). No observable change: the base hooks do nothing.
   One corner: a hook assigned as an instance attribute (`self.pre = ...`) on a class that doesn't
   override it would now be skipped; no example or test does that.
5. **Observers always fail open** (decision 3) and **`FsError` is an answer** (decision 4): both today's
   behaviour, kept.
6. **`require_async` not used** (decision 5): `check_coroutine` keeps its `RouteError` message.
7. **The per-call cost** above. Recommended: accept it, and raise caching the `skip_inherited` lookup in
   interpose.
