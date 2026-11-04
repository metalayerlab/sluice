# TODO

Short-lived working list. Bugs and features with discussion belong in GitHub issues; this file is for what
the team (and its AI assistants) should know at a glance.

## Open

- [ ] Link Troubleshooting's `auth()` import entry to the FAQ anchor `#where-did-auth-and-access_log-go` when `check-wiki.py` keeps inline code in heading anchors (metalayerlab/project-template#83)

## Decided against (don't re-propose without reading this first)

<!-- One line each: the idea, why not, and a link to the issue, PR or design record. For example:
- Helm chart: kustomize base + Flux overlays cover our clusters; a chart doubles the maintenance (design 0003).
-->
- fusepy / refuse / llfuse: synchronous or superseded; pyfuse3 instead (design 0001).
- Rewrite-as-middleware for the real path: depends on list order; backend is a route property (design 0001).
- Per-call env/secret resolution: I/O on every FUSE call; frozen at mount time (design 0001).
- Built-in middleware in the package (`auth()`, `access_log()`, `hide()`, ...): policies ship as `examples/`, the package ships mechanism (design 0002).
- `sluice.contrib` subpackage for examples: the same API-surface problem under another name (design 0002).
- Own settings engine or `Secret` beside interpose: the copies drift; mechanism lives in interpose (design 0003).
- Pinning interpose via `[tool.uv.sources]` only: a built wheel would look `interpose` up on PyPI (design 0003).
- `env.expose` name rules in interpose: sluice policy, not mechanism (design 0003).
- YAML projection: `: > leaf` as a documented no-op: one request would commit or not by the kernel's flags, silently (design 0012).
- YAML projection: checking an `O_TRUNC` open's empty buffer at `open`: `echo x > leaf` would fail before its value arrived (design 0012).
- YAML projection: typing written list items as YAML would (`3` an integer): the projector would parse YAML, and YAML 1.1 turns `No`, `on`, `007` into other types (design 0012).
- YAML projection: one item type for a whole list: a mixed list still loses its types on an unchanged write (design 0012).
- YAML projection: checking at write only a buffer that ends in a newline: `printf bad > leaf` would pass `write` and fail only at `close` (design 0012).
- YAML projection: comparing a chunk with the mount's own maximum write: the handler can't see it, and the kernel can split up to a page early (design 0012).
- YAML projection: a prefix grammar per type, failing only what no later write could fix: a grammar for every type, and `validate` can't run on a prefix (design 0012).
