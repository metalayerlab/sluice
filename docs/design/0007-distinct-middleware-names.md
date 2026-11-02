# 0007: Distinct names for repeated middleware

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved by Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

## Context

Design 0004 names each pipeline entry by its middleware's `repr`, and didn't require names to be
unique (interpose 0003, decision 9). But interpose keys per-call `Run.timings`
by entry name, and design 0005 uses those names as the `middleware` label of
`sluice_middleware_seconds_total` and `sluice_middleware_failures_total`. When a frozen chain holds
the same name twice (the same middleware registered globally and on a route, two instances of one
class, or two plain functions with one qualified name), their time is added into one series and their
failures can't be told apart.

## Decision

- **Within one frozen chain, the first entry with a name keeps it; the second and later get `name#2`,
  `name#3`, ...**, in chain order (global middleware first). A numbered name already in the chain (a
  middleware whose `repr` is literally `x#2`) is skipped: the next free number is used.
- Names are assigned in `compile_table`'s second pass, as each node's own entries are appended to its
  inherited chain. An ancestor's entries are shared with its descendants (design 0004) and keep their
  names, so a name never changes with the route that matched. Another chain without the repeat uses the
  plain name.
- The metrics' `middleware` label is declared from the entries of the frozen tree, as before, so it
  holds the numbered names too: counting them is never refused.
- `sluice routes` and `sluice resolve` still print each middleware's `repr`; the number appears where
  entry names are used: the `failed`-hook and timeout log lines (design 0006) and the metrics.

## Alternatives considered

- **Keep shared names** (0004). Merges two middleware's time and failures into one series.
- **Number every entry** (`auth#1`). Renames every existing series for the common case of no repeat.
- **Refuse repeated names at freeze.** Registering one middleware twice is legitimate (it runs twice,
  0004), and a class with a shared `repr` is common.
- **Number by registration across the whole tree.** A name would then depend on unrelated routes; per
  chain, it depends only on what runs before it in the call.

## Consequences

- Repeated middleware get their own timings and metric series; dashboards that summed the shared
  series now see `name#2` beside `name`.
- Rollback: revert; nothing persists.
- Wiki: Architecture-Internals (entry names), In-Depth-Configuration (the `middleware` label).

## Open questions

None: approved by Stephen on 2026-11-02.
