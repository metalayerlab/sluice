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
