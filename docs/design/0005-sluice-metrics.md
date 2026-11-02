# 0005: sluice metrics

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved under Stephen's overnight delegation; Stephen to confirm on review)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude

## Context

sluice has no metrics. Its only signals are logs, so "how many calls fail, with what, and is a
middleware slow or crashing" means grepping. interpose v0.1.1 ships the mechanism
([design 0002](https://github.com/metalayerlab/interpose/blob/v0.1.2/docs/design/0002-metrics.md):
counters, histograms, the Prometheus text format, and labels whose values come from declared sets),
and its "Consumer adoption" brief for sluice asks for a record that picks the metrics and serves them as
`/.sluice/metrics`. Design 0004 already gives each call's per-middleware hook times (`Run.timings`) and
every hook failure (`on_failure`).

Constraints:

- **No HTTP.** Design 0001 rejected a control or metrics API (a network surface and a dependency for
  nothing sluice needs) and suggested status as virtual files, with `/.sluice/health` as its example.
- **No per-call I/O** (TODO.md, "Per-call env/secret resolution"). Counting is memory only.
- **Bounded series.** A label value comes only from a set declared up front: never a path, a route
  param, a caller, or anything else a call supplies.
- **No change when it's off.** Existing mounts see the same tree and the same behaviour.

## Decision

### The metrics

| Metric | Type | Labels | What |
|---|---|---|---|
| `sluice_call_duration_seconds` | histogram | `op` | Every filesystem call through the dispatcher, failed ones included: its `_count` is the per-operation call count |
| `sluice_call_errors_total` | counter | `op`, `errno` | Calls that failed, by the errno returned to the kernel |
| `sluice_middleware_seconds_total` | counter | `middleware` | Time spent in each middleware's hooks |
| `sluice_middleware_failures_total` | counter | `middleware`, `hook` | Hooks that raised something other than an `FsError` |

Label values, each from a declared set:

- **`op`**: `OPS`, the 23 operations middleware sees. `forget`, `release`, `releasedir` (bookkeeping
  that bypasses middleware) and `link`/`mknod` (always `ENOTSUP`) aren't counted.
- **`errno`**: the names in Python's `errno.errorcode` (`ENOENT`, `EACCES`, ...), plus `unknown` for
  an `FsError` whose number isn't in that table. The errno is the one `fs.py` replies with: the
  `FsError`'s, or `EIO` for any other exception.
- **`hook`**: `pre`, `post`, `failed`.
- **`middleware`**: the names of every middleware entry in the frozen route tree (design 0004: its
  `repr`, as `sluice routes` prints it), declared when the tree is frozen. Configuration, not traffic.

Buckets are fixed (a dashboard contract): 0.1 ms to 5 s, 15 bounds, from a cached-metadata call to a
slow backend or network-backed middleware.

A deny (`Deny`, any `FsError` from a hook) is an answer, not a failure: it shows in
`sluice_call_errors_total`, never in `sluice_middleware_failures_total`.

### Where it is counted

- **Calls**, in the `Dispatcher`: each of its 23 operation methods is wrapped once (`_measured`), so a
  routing failure (`ENOENT` for a path no route covers, `EXDEV` for a cross-backend rename) counts too,
  though it never reaches the pipeline. `readdir` counts each page the kernel asks for.
- **Middleware time and failures**, in `run_pipeline`: `Run.timings` is added to
  `sluice_middleware_seconds_total` when the call ends, and `on_failure` counts every hook failure,
  including the `failed` hooks the pipeline ignores.

Without metrics, the `Dispatcher` wrapper returns straight away and `run_pipeline` skips both, so a
mount with metrics off does nothing new per call.

### The file

`/.sluice/metrics` is a read-only virtual file whose content is `Registry.render()`, rendered when the
file is opened (`VirtualHandler.render`, once per open, like every virtual file). The kernel reads
that snapshot, so a scrape sees every call that finished before its `open`. Prometheus scrapes it
through node_exporter's textfile collector (`cp` it into the collector's directory on a timer) or any
reader of the mount.

- It is a route like any other, added under the root when the tree is frozen, so **global middleware
  (`app.use()`) runs on it**: an authorisation hook gates the metrics as it gates everything else.
- `/.sluice` is a grouping route of its own, so it is a synthetic directory even when the app has a
  root `{param}` route (literal segments win over params). The root listing shows `.sluice`.
- Write operations fail with `EROFS` (the `VirtualHandler` defaults).

### The namespace

sluice didn't reserve `/.sluice/` before this record; 0001 named it only as a suggestion. With metrics
on, it becomes sluice's: **any app route whose first segment is the literal `.sluice` is a
`RouteError`** (`/.sluice is reserved for sluice's own files while metrics.enabled is on`). A dot
name stays out of `ls` and glob listings, it is named after the project so it can't be mistaken for
data, and it leaves room for `/.sluice/health` and later files without another reservation. Refusing
the whole prefix rather than the one path follows 0001's rule (question 8): relaxing it later breaks
nobody; tightening it would. With metrics off nothing is reserved, so no existing app breaks.

### The setting

| Key | Variable | Type | Default |
|---|---|---|---|
| `metrics.enabled` | `SLUICE_METRICS_ENABLED` | bool | `false` |

**Opt-in**, as every sluice feature that changes what a mount shows or does is off by default
(`mount.allow_other`, `mount.debug`, `pipeline.slow_ms` at `0`). Turning it on adds a directory to the
mount's root and reserves a namespace an app may already use; on by default, upgrading could refuse an
existing route tree. `prepare()` builds one `Metrics` when it is on and passes it to
`Sluice.freeze(config, metrics=...)`, a new keyword-only argument (default `None`). The tree is not
modified, so freezing again (another command, a test) is unaffected.

## Alternatives considered

- **An HTTP `/metrics` endpoint.** Rejected by 0001; the mount is already the interface.
- **A node_exporter textfile written by sluice.** I/O on a timer, a path to configure, and stale files
  after a crash. Copying the virtual file on a timer gives the same result outside sluice.
- **`sluice_call_duration_seconds{op,outcome}`** (interpose's suggestion). An `ok`/`error` outcome
  label on the histogram splits every bucket series in two; the errors counter carries the errno, which
  is what an operator asks for, and the histogram's count minus the errors gives the successes.
- **A middleware-duration histogram.** One series per bucket per middleware; a seconds counter gives
  the rate and share of time at a fraction of the series. A histogram can follow if tails matter.
- **Counting in `fs.py`'s guard.** It is pyfuse3-only and untestable without libfuse; the dispatcher
  sees the same calls and the same errnos.
- **Counting in `run_pipeline` only.** Misses routing failures, the most common `ENOENT`.
- **Reserving only `/.sluice/metrics`.** A later `/.sluice/health` would then break apps that had
  meanwhile put routes under `/.sluice`.
- **On by default.** See [The setting](#the-setting).
- **A numeric `errno` label** for unknown codes: unbounded in principle; `unknown` is one value.

## Consequences

- One setting, one module (`sluice/metrics.py`), and a small per-call cost with metrics on: a histogram
  observation, at most one counter increment per call, and one per middleware that ran. Measured on
  the workstation with five global middleware, `run_pipeline` alone goes from about 28 to 30 µs per
  call; the dispatcher wrapper adds two clock reads and a histogram observation.
- `Resolution` and `RouteTable` gain a `metrics` field (`None` when off); `Sluice.freeze` gains
  `metrics=`. Nothing else in the public API changes.
- With metrics on, an app with routes under `/.sluice` fails to start with a one-line `RouteError`.
- The file is readable by whoever can read the mount and passes global middleware. It reveals call
  volumes, error rates and middleware names, never paths or users.
- Rollback: turn the setting off, or revert; nothing persists.
- Wiki: In-Depth-Configuration (the setting, a section), Architecture-Internals (the module, where
  counting happens), Security (who can read it), Deployment (scraping), Troubleshooting (the
  reserved-namespace error), Testing-Guide (the tests); `contrib/sluice.example.yaml`.

## Open for Stephen

Each is reversible.

1. **Opt-in** (`metrics.enabled: false`). Recommended, for the reasons above.
2. **`/.sluice` reserved as a whole while on.** Recommended (0001, question 8).
3. **The four metrics and the bucket layout.** Recommended as a small first set; adding metrics later
   breaks nobody, renaming or re-bucketing does.
4. **Global middleware gates the file.** Recommended: no second access path that skips the app's
   policy. The cost is that a deny-everything global middleware hides the metrics too.
5. **No `sluice_build_info` or process metrics.** Recommended for now: `sluice version` and the
   container image tag already say what runs.
