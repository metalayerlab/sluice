# sluice

[![CI](https://github.com/metalayerlab/sluice/actions/workflows/ci.yml/badge.svg)](https://github.com/metalayerlab/sluice/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/metalayerlab/sluice?sort=semver)](https://github.com/metalayerlab/sluice/releases)
[![Image](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/metalayerlab/sluice/pkgs/container/sluice)

Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores.

<!-- guidance: two or three sentences: what problem this solves and for whom. Then delete this comment. -->

## Quick Start

```sh
uv tool install git+https://github.com/metalayerlab/sluice
sluice --help
```

The full walk-through is in the wiki's [Quick Start](https://github.com/metalayerlab/sluice/wiki/Quick-Start).

## Documentation

Everything is in the **[wiki](https://github.com/metalayerlab/sluice/wiki)**. Its source is [`docs/wiki/`](docs/wiki/), in this
repository, reviewed with the code.

- [Introduction](https://github.com/metalayerlab/sluice/wiki/Introduction): what it is and how it works
- [In-Depth Configuration](https://github.com/metalayerlab/sluice/wiki/In-Depth-Configuration): every setting
- [Troubleshooting](https://github.com/metalayerlab/sluice/wiki/Troubleshooting) and [FAQ](https://github.com/metalayerlab/sluice/wiki/Frequently-Asked-Questions)
- [Contributing](https://github.com/metalayerlab/sluice/wiki/Contributing) and [Release Process](https://github.com/metalayerlab/sluice/wiki/Release-Process)

## For AI assistants

Built with AI assistants; every change gets human review. **[AGENTS.md](AGENTS.md) is the only instruction
file** (stack, commands, rules, definition of done, docs map). Read it instead of this README.

| Assistant | Entry point |
|---|---|
| Claude Code | [CLAUDE.md](CLAUDE.md) (imports AGENTS.md) + [.claude/settings.json](.claude/settings.json) |
| Codex, Cursor, Copilot coding agent | AGENTS.md, natively ([Copilot](.github/copilot-instructions.md), [Cursor](.cursor/rules/project.mdc) pointers) |
| Gemini CLI | [GEMINI.md](GEMINI.md) (imports AGENTS.md) |
| Chat without repo access | [docs/ai/prompts.md](docs/ai/prompts.md), "Context for a chat assistant" |

Humans: kick-off prompts are in [docs/ai/prompts.md](docs/ai/prompts.md), and the review checklist in
[docs/ai/review-checklist.md](docs/ai/review-checklist.md).

## Development

```sh
uv sync                           # Python >= 3.13, dev tools included
uv run pytest                     # tests (80% coverage floor)
uv run ruff check && uv run mypy  # lint and type check
```

The workflow is: a branch, Conventional Commit PR titles, squash merge. Releases are automated by
release-please. See [Contributing](https://github.com/metalayerlab/sluice/wiki/Contributing).

This project was generated from [project-template](https://github.com/metalayerlab/project-template).
Template fixes arrive as a weekly *Template update* PR.

## License

Released into the public domain under the [Unlicense](LICENSE).
