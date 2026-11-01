# Design records

Why the code is the way it is. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` before a
new feature, a new dependency, or a public-interface change, and add a line below. Status moves Proposed →
Approved (by a human) → Implemented; supersede an implemented record with a new one, never rewrite it.

**Index:** read this first, then open only the records relevant to your task. CI checks that every record is listed.

| # | Title | Status | Decision in one line |
|---|---|---|---|
| [0001](0001-initial-design.md) | Initial design | Implemented | pyfuse3 mount with a Python-defined route tree (own / inherited / virtual backends), phase-structured pre/post middleware, mount-time env and secrets; CLI `mount`, `routes`, `resolve`; no HTTP. |
| [0002](0002-strmfs-and-shadowfs-on-sluice.md) | Middleware lives in examples/; strmfs and ShadowFS on sluice | Approved | No concrete middleware in `src/` (`auth()`/`access_log()` move to `examples/`, supersedes part of 0001); add `ctx.backend` and errno mapping in hooks; strmfs, rules, layers (relative symlinks), transforms ship as examples; `names=`/`resolver=` deferred. |
