# TODO

Short-lived working list. Bugs and features with discussion belong in GitHub issues; this file is for what
the team (and its AI assistants) should know at a glance.

## Open

- [ ] Verify the container recipe in the wiki's Deployment page (image build and a mount inside Docker), then remove its `TODO(wiki)` marker (a Release PR is blocked until then)
- [ ] Remove the Dockerfile's libfuse3 apt workaround, and add an integration job to CI, once metalayerlab/project-template#53 (system-packages hook) lands; design 0001, question 10

## Decided against (don't re-propose without reading this first)

<!-- One line each: the idea, why not, and a link to the issue, PR or design record. For example:
- Helm chart: kustomize base + Flux overlays cover our clusters; a chart doubles the maintenance (design 0003).
-->
- fusepy / refuse / llfuse: synchronous or superseded; pyfuse3 instead (design 0001).
- Rewrite-as-middleware for the real path: depends on list order; backend is a route property (design 0001).
- Per-call env/secret resolution: I/O on every FUSE call; frozen at mount time (design 0001).
