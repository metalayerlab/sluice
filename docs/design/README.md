# Design records

Why the code is the way it is. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` before a
new feature, a new dependency, or a public-interface change, and add a line below. Status moves Proposed →
Approved (by a human) → Implemented; supersede an implemented record with a new one, never rewrite it.

**Index:** read this first, then open only the records relevant to your task. CI checks that every record is listed.

| # | Title | Status | Decision in one line |
|---|---|---|---|
| [0001](0001-initial-design.md) | Initial design | Implemented | pyfuse3 mount with a Python-defined route tree (own / inherited / virtual backends), phase-structured pre/post middleware, mount-time env and secrets; CLI `mount`, `routes`, `resolve`; no HTTP. |
| [0002](0002-strmfs-and-shadowfs-on-sluice.md) | Middleware lives in examples/; strmfs and ShadowFS on sluice | Implemented | No concrete middleware in `src/` (`auth()`/`access_log()` move to `examples/`, supersedes part of 0001); add `ctx.backend` and errno mapping in hooks; strmfs, rules, layers (relative symlinks), transforms ship as examples; `names=`/`resolver=` deferred. |
| [0003](0003-adopt-interpose-for-settings-and-secrets.md) | Adopt interpose for settings and secrets | Implemented | Settings engine, `Secret` and `read_secret_dir` come from interpose v0.1.0 (a GitHub tag); keys and variables unchanged; single-value rules as `check=`, `env.expose` a list setting with its rules kept in sluice. |
| [0004](0004-adopt-interpose-pipeline-core.md) | Adopt interpose pipeline core | Implemented | `run_pipeline` drives interpose v0.1.2's `Pipeline`, frozen per route, with a `Policy` that is today's semantics (fail closed, onion `post`, `FsError` an answer, every hook awaited); failures mapped through the existing errno mapping and logged once; no timeout yet. |
| [0005](0005-sluice-metrics.md) | sluice metrics | Implemented | Opt-in `metrics.enabled`: per-op call counts and durations, errors by errno, middleware time and failures (interpose metrics, declared label sets only), served as the read-only virtual file `/.sluice/metrics`, rendered per open; `/.sluice` reserved while on. |
