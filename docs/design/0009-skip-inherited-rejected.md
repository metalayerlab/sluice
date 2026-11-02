# 0009: skip_inherited rejected for now

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Superseded by 0010 (Rejected, Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

Supersedes [0008](0008-skip-inherited-hooks.md).

## Context

Design 0008 turned on interpose's `skip_inherited=Middleware`, so that hooks a middleware leaves as the
base class's no-ops are no longer called. Its benchmark (`benchmarks/bench_pipeline.py`: `run_pipeline`
with five middleware, no-op bodies, interpose v0.1.5, best of three alternating runs of 100 000 calls,
three runs) showed it was slower:

| Chain | Every hook awaited | `skip_inherited` |
|---|---|---|
| typical (2 of 10 hooks are no-ops) | 13.1–13.8 µs | 14.8–15.6 µs |
| five `pre`-only gates (5 of 10 are no-ops) | 12.3–13.1 µs | 12.8–13.7 µs |

interpose v0.1.5 made the guarded no-op await cheap. The per-call check that decides whether to skip a
hook (an instance `__dict__` lookup and two class lookups, for every hook) now costs more than the await
it saves.

## Decision

- `skip_inherited` is **off** in `POLICY` again: every hook is awaited, the base class's no-ops
  included. Every middleware that matched a call gets its `sluice_middleware_seconds_total` series, as
  before 0008.
- Try again once interpose resolves overrides once per entry instead of per call
  ([metalayerlab/interpose#25](https://github.com/metalayerlab/interpose/issues/25)). Re-run
  `benchmarks/bench_pipeline.py` then, and turn it on only if it is faster.
- The tests that 0008 added for instance hooks, `failed`-only observers and `respond()` from a
  `param()` callback stay. They hold either way.

### Correction to 0006

0006 says a hook that swallows `CancelledError` "keeps running past its limit and the call is not
failed". Since interpose v0.1.4 (its design 0005) that is wrong: a result that comes back after the
limit is discarded and treated as a timeout failure, so the call fails with `EIO`. The log line says
`(it swallowed the cancellation)`, or `(it blocked the event loop past the limit)` for a hook that
never reached an `await`. The wiki already describes this behaviour.

## Alternatives considered

- **Keep it on.** It costs about 0.5–1.5 µs per call for no gain.
- **Skip in sluice at freeze time.** That needs a second chain per hook (0008, Alternatives), and the
  fix belongs in interpose.

## Consequences

- Same behaviour and metrics as v0.2.2. Per-call cost goes back to the faster numbers above.
- TODO.md lists it under "Decided against", for now.
- Wiki: Architecture-Internals, In-Depth-Configuration (the metric row), Testing-Guide.
