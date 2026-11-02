# 0008: Skip hooks a middleware doesn't override

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved by Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

## Context

`Middleware` gives every middleware three hooks, and most override only one or two (an auth gate
overrides `pre` only). sluice awaits every hook, the base class's no-ops included, each through
interpose's guard. Design 0004 left interpose's `skip_inherited` off for two reasons (open question 4):
a hook set on the instance (`mw.pre = gate`) would have been skipped, so the gate failed open, and a
skipped `pre` wasn't followed by the `until` check, so a `ctx.respond()` from a `param()` callback let
later `pre` hooks run. interpose v0.1.3 (metalayerlab/interpose#12) fixed both: an instance attribute
counts as an override, and `until` is asked after a skipped call too. sluice is on v0.1.5.

## Decision

- `POLICY` gains `skip_inherited=Middleware`. A hook whose implementation is `Middleware`'s own (not
  overridden in the class, not set on the instance) isn't called: no await, no guard, no timeout,
  no timing. `post` passes the result through, `pre` is followed by the `ctx.responded` check, and the
  entry still counts as having run, so its `post` and `failed` hooks are called as before.
- `FunctionMiddleware` overrides `pre`, so a plain `async def` is unaffected.
- **Metrics.** `sluice_middleware_seconds_total` only times hooks that are called. A middleware has no
  series until one of its own hooks has run (a `failed`-only observer has none until a call fails),
  where before every middleware that matched a call got a near-zero series. Tests and dashboards that
  expected a series for every middleware on every call must not. No sluice test or doc depended on it;
  In-Depth-Configuration now says so. `sluice_middleware_failures_total` is unchanged: a skipped no-op
  can't fail.

## Benchmark

`benchmarks/bench_pipeline.py`: `run_pipeline` with five middleware, no-op hook bodies and a no-op
operation, best of three alternating runs of 100 000 calls each, on the workstation with Python 3.13.15
and interpose v0.1.5. Three runs in total:

| Chain | Every hook awaited | `skip_inherited` |
|---|---|---|
| typical: auth gate (`pre`), access log (`pre`/`post`/`failed`), two rewriters (`pre`/`post`), a function gate; 2 of 10 hooks are no-ops | 13.1–13.8 µs | 14.8–15.6 µs |
| five `pre`-only gates; 5 of 10 hooks are no-ops | 12.3–13.1 µs | 12.8–13.7 µs |

**It is slower, by about 1.5 µs and 0.5 µs per call.** interpose v0.1.5 (its design 0006) made the guard
around a no-op await cheap, so skipping one saves less than the per-hook check that decides to skip it
(an instance `__dict__` lookup and two class lookups, done for every hook, overridden or not). Against
the 28 µs that design 0004 measured on v0.1.2, the difference is small either way.

## Alternatives considered

- **Leave it off** (0004). Faster on v0.1.5, per the benchmark, and every middleware keeps a timing
  series on every call. The author's recommendation; see below.
- **Skip in sluice** (filter entries per hook at freeze time). The skip would then cost nothing per call,
  but the selection by `ops` and `failed` reaching every entry that ran would need a second chain per
  hook: more code than the saving is worth.

## Consequences

- The base class's no-op hooks are never awaited, so they can't time out or be cancelled.
- Per-call cost: about 0.5 to 1.5 µs more with five middleware on interpose v0.1.5 (above).
- Metric series appear only for middleware whose hooks ran (above).
- Rollback: drop `skip_inherited` from `POLICY`.
- Wiki: Architecture-Internals, In-Depth-Configuration (the metric), Testing-Guide.

## Open questions

1. **Keep it on, given the benchmark?** Stephen approved this on 2026-11-02, before the numbers were
   in. Recommendation: revert to off (one line) unless interpose makes the check cheaper, for example
   by deciding per entry once at `Pipeline` construction rather than per call. Worth raising in
   interpose.
