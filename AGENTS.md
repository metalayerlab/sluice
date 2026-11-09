# AGENTS.md

sluice: Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores.
Single source of instructions for AI assistants; the tool files point here. Read [`.agents/stack.md`](.agents/stack.md) with it: what this project is, its map, conventions and checks.

## How to work

1. Changing a convention is the user's call: propose it, don't apply it.
2. A decision (layout, conventions, sources, dependencies, a public interface) gets a design record where `.agents/stack.md` says: **stop for approval**.
3. A human reviews every change. Report what you skipped or couldn't check; never claim an unrun check passed.
4. Asking the user something? Give your recommendation and why.
5. Reply tersely: the result, then what you need from the user. No preamble, recaps or restated plans.
6. Ending a session? List what you left (worktrees, local and remote branches, temp files, processes, containers), whether each is safe to remove and why, and remove only what the user approves.

## Don't read

- `.graphify/`, `graphify-out/`, the paths `.agents/stack.md` lists, and what the project marks as evidence, generated or archived.

New large or generated path? List it in `.agents/stack.md` and deny `Read` on it in `.claude/settings.json`.

## Repository map

T = template-owned: never edit here; fixes arrive by update PR. A bug in one goes to the template that owns it: `_src_path` in its `.copier-answers.*.yml` (see [Never](#never)).

| Path | What |
|---|---|
| `.agents/stack.md` | This project's stack: map, conventions, checks (T) |
| `.github/workflows/`, `scripts/` | CI and its scripts (T), except what `.agents/stack.md` marks as the project's |
| `.gitleaks.toml` | The secret scan's allow-list: the project's own entries, each with why. It extends `.gitleaks.base.toml` (T) |
| `.copier-answers.*.yml` | Copier only |

## Never

- Push to `main`, create tags or releases, or edit `.copier-answers.*.yml`. Every other `gh` command is allowed.
- Change another codebase from here (repos, submodules, dependencies), even when asked: the user opens a session there. Search its issues (`gh issue list -R <owner/repo> --state all -S <terms>`), comment your case or draft one for the user to confirm in its issue form (or the owner's `.github` repo), every field. None? Hand-write only on the user's yes, saying why. A workaround stays minimal, links the issue next to it, and goes when the fix lands.
- Credit an AI in commits or PRs: no co-author trailers, bylines or session links.
- Commit secrets or what `.gitignore` keeps out (evidence, keys, private folders).
- Weaken a check: loosen `scripts/checks.sh`, a `scripts/checks.d/` check or the secret scan, add `continue-on-error`. If a check is wrong, say so.
- Grow this file, `.agents/stack.md` or the tool files past the context budget (`sh scripts/checks.sh`, CI-checked): be terse.

## Definition of done

```sh
sh scripts/checks.sh
scripts/check-docs-secrets.sh  # if Docker is available
```

All pass.
