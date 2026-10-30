# Contributing

<!-- AI assistants: AGENTS.md holds these rules in short form. Read further only for the reasons behind a rule. -->

## Development setup

```sh
git clone https://github.com/metalayerlab/sluice && cd sluice
uv sync --extra fuse                     # Python >= 3.13; pyfuse3 needs libfuse3-dev, pkg-config, gcc
uv tool install pre-commit && pre-commit install -t pre-commit -t commit-msg
uv run pytest                            # all green before you start
```

## Workflow

1. Branch from `main`: `feat/<topic>` or `dev/<topic>`. Pushes build `:<branch>` images.
2. Non-trivial change (a new feature or dependency, or a change to a public interface): write a [design record](https://github.com/metalayerlab/sluice/tree/main/docs/design) first, and get it approved.
3. Open a PR against `main` with a **Conventional Commit title** (`feat: add retry to uploads`). It becomes the squash commit and the CHANGELOG line: see [Release Process](Release-Process).
4. CI must be green: tests, lint, types, secret scan, manifests, and the wiki checks.
5. A code owner reviews ([review checklist](https://github.com/metalayerlab/sluice/blob/main/docs/ai/review-checklist.md)), then squash-merge.
   The only code owner and also the author? GitHub won't let you approve your own PR. Review it against the checklist yourself, then, if `main`'s ruleset lets admins bypass it, merge with `gh pr merge --squash --admin` once CI is green. Never push to `main` instead.

Never edit `CHANGELOG.md` or the version, and never push tags: release-please does that.

## Code style

- ruff (lint + format, line length 100) and mypy `--strict` decide formatting and typing. Don't argue with them in review.
- Comments explain *why*. Logging goes through `logging`; only the CLI prints.
- Settings go through `config.SETTINGS`. Secrets come from `_FILE`, and are never logged.
- Prefer the standard library and existing dependencies. Justify every new one in the PR.

Rules specific to sluice:

- **Never block the event loop.** One asyncio loop serves every FUSE call. Backend syscalls go through `Passthrough.run()` (the worker pool). Nothing in `dispatch.py`, `middleware.py` or a handler does synchronous I/O.
- **pyfuse3 stays in `fs.py` and `mount.py`.** Everything else must import and be tested without it, because CI has no libfuse. `fs.py` converts and does nothing else: logic goes in the dispatcher.
- **Errors for one call are `FsError(errno)` or `Deny`.** Never let another exception escape to pyfuse3: it would stop the mount. Startup errors are `ConfigError`, `AppLoadError`, `RouteError` or `MountError`, with a one-line message that names the setting or route pattern.
- **Mask what is secret.** Log `ctx.display_path`, never `ctx.path` or raw params. Never log a `Secret`'s `.reveal()`. The integration test fails if a token reaches the daemon's log.
- **No `ENOSYS` from defaults.** The kernel remembers it for the whole mount (see [Architecture Internals](Architecture-Internals#design-decisions)).
- **Operation names** are the FUSE names (`getattr`, `readdir`, ...), listed in `middleware.OPS`. A new op is added there, in the dispatcher, in `fs.py`, and in both `VirtualHandler` and `Passthrough`.
- **Run the integration tests** (`uv sync --extra fuse`, `RUN_INTEGRATION=1`) before a PR that touches `fs.py`, `mount.py` or the dispatcher. CI can't. See [Testing Guide](Testing-Guide).

## Working with AI assistants

AI assistants are welcome and expected, under the same rules as anyone else:

- They follow [AGENTS.md](https://github.com/metalayerlab/sluice/blob/main/AGENTS.md). Each tool's own file (`CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.cursor/rules/`) points there.
- Kick-off prompts: [docs/ai/prompts.md](https://github.com/metalayerlab/sluice/blob/main/docs/ai/prompts.md).
- The PR template asks how much was generated. Say so honestly: it tells the reviewer how closely to read.
- An assistant that asks you something gives its own recommendation and why, so you can say "yes" or argue with a stated reason instead of having to ask. It still waits for your answer.
- A human who understands the change approves every PR. "The AI wrote it" is never a reason to merge.
- A bug in a template-owned file is reported to project-template, not only worked around here: every other project has it too. The steps are in the template wiki's [Updating from the Template](https://github.com/metalayerlab/project-template/wiki/Updating-from-the-Template#reporting-a-template-problem).

## Documentation

This wiki's source is [`docs/wiki/`](https://github.com/metalayerlab/sluice/tree/main/docs/wiki), in the repository. It's published to the GitHub wiki from `main`, so **never edit the GitHub wiki directly**: the next publish overwrites it.

- Code and docs change in the same PR. AGENTS.md's "Docs map" says which page goes with which code, and CI fails a PR that changes watched paths without touching `docs/wiki/`. A reviewer, never the author, can add the `no-docs-needed` label when nothing user-visible changed.
- The required pages and headings are in `docs/wiki/.standard.toml` (template-owned). Add pages freely, and link each one from `_Sidebar.md`.
- Tables between `<!-- generated:start/end -->` markers come from the code: run `uv run scripts/gen-wiki.py`.
- Link pages by name (`[Quick Start](Quick-Start)`), and repo files by full URL.
- Check locally: `python3 scripts/check-wiki.py`.

### Keeping AI context small

Assistants pay for what they read in every session, so the instruction files are written for that:

- `AGENTS.md` holds the *rules*, tersely. The *reasons* live on these wiki pages. CI fails if AGENTS.md or a tool pointer file (`CLAUDE.md`, `GEMINI.md`, Copilot, Cursor) exceeds `[context_budget]` in `docs/wiki/.standard.toml`. That budget is template-owned: if it's genuinely too small, change it in project-template.
- AGENTS.md's "Don't read" list (and the `Read` denies in `.claude/settings.json`) keep lock files, caches and generated output out of context. Add any large or generated path the project introduces.
- `docs/design/README.md` indexes every design record, so an assistant reads one line per record instead of every record. CI checks the index is complete.
- Pointer files only point: don't copy AGENTS.md content into them.
- The checks print one line when they pass, and their detail only when they fail (`check-wiki.py --verbose` lists the warnings it counts). Keep new scripts that way: an assistant reads their output on every run.
- Every turn sends the whole conversation again. Start each task in a new session (the assistant suggests it when one ends), and point to a file or a failing check by name instead of pasting it.
- Assistants reply tersely: the result, then what they need from you. Terse means no preamble or recaps, not fewer reasons: recommendations and what was skipped still belong in the reply.
