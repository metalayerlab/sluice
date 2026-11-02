# 0010: skip_inherited back on, resolved once per pipeline

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved by Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

Supersedes [0009](0009-skip-inherited-rejected.md), reinstating [0008](0008-skip-inherited-hooks.md).

## Context

0009 turned `skip_inherited` off because interpose v0.1.5 decided whether to skip a hook on every call
(an instance `__dict__` lookup and two class lookups per hook), which cost more than the guarded no-op
await it saved. It said to retry once interpose resolved overrides once per entry
([metalayerlab/interpose#25](https://github.com/metalayerlab/interpose/issues/25)), and to turn it on only
if faster. interpose v0.2.2 ([#28](https://github.com/metalayerlab/interpose/pull/28)) does: the
`Pipeline` works out each entry's inherited hooks when it is built, and the guard asks a dict and a set.
v0.2.1 is documentation only.

## Decision

- The interpose pin moves from v0.2.0 to v0.2.2, and `POLICY` gets `skip_inherited=Middleware` back:
  0008's behaviour and metrics, instance hooks and the `ctx.responded` check after a skipped `pre`
  included.
- **New semantics.** Overrides are resolved when `compile_table` builds each `Pipeline` (at
  `freeze()`). A hook assigned on a middleware instance or class after that isn't seen, so a gate
  assigned late would never run. One assigned before is honoured. sluice never assigns hooks at
  runtime: `src/`, `examples/` and `benchmarks/` don't, and the one test that does
  (`mw.pre = gate`) assigns it before freezing. Writing-an-App says to assign hooks before.

## Benchmark

`benchmarks/bench_pipeline.py` as in 0008: µs per call, best of three alternating runs of 100 000
calls, on the workstation with Python 3.13.15. v0.2.0: three runs; v0.2.2: five.

| Chain | interpose | Every hook awaited | `skip_inherited` |
|---|---|---|---|
| typical (2 of 10 hooks are no-ops) | v0.2.0 | 13.4–14.3 µs | 14.9–16.0 µs |
| typical | v0.2.2 | 13.1–14.2 µs | 12.9–13.9 µs |
| five `pre`-only gates (5 of 10 are no-ops) | v0.2.0 | 12.9–13.1 µs | 13.1–13.7 µs |
| five `pre`-only gates | v0.2.2 | 12.7–13.7 µs | 10.9–11.9 µs |

On v0.2.2 `skip_inherited` is about 1.3–2.3 µs faster on the gates chain. On the typical chain the
two are level, within the noise of a run (−0.7 to +0.1 µs per run). It is no longer slower anywhere, so it
goes back on.

## Alternatives considered

- **Leave it off** (0009). Every middleware keeps a series on every call, but it gives up the
  saving on chains of `pre`-only gates, the common case for authorisation.

## Consequences

- 0008's consequences again: base no-op hooks aren't awaited, timed or cancelled, and a middleware has
  no `sluice_middleware_seconds_total` series until one of its own hooks has run.
- A hook assigned after `freeze()` is ignored (above).
- TODO.md drops the "Decided against" line for it.
- Rollback: drop `skip_inherited` from `POLICY`; the pin can stay.
- Wiki: Architecture-Internals, In-Depth-Configuration (the metric row), Writing-an-App, Testing-Guide.
