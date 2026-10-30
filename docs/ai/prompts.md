# Kick-off prompts

Copy-paste prompts for starting work with an AI assistant. They assume the assistant can read the
repository (Claude Code, Codex, Copilot agent, Cursor, Gemini CLI). For a chat with no repo access, start
with [Context for a chat assistant](#context-for-a-chat-assistant).

Replace `<...>`. Each prompt ends at a point where a human looks before work continues: keep that stop.
Start each task in a new session, and point to files or a failing check by name rather than pasting them:
the whole conversation is sent again with every turn.

## 1. First build-out from the skeleton

Use this once, right after `copier copy`.

```text
This repository was just generated from our project template. Read AGENTS.md
fully, then docs/wiki/.standard.toml, docs/design/0000-template.md and TODO.md.

The project: <what it must do, who uses it, the inputs and outputs, the
constraints: performance, deployment target, integrations>.

Step 1, and stop after it: write docs/design/0001-initial-design.md from the
template, and list it in docs/design/README.md. Cover the components, data flow, configuration settings (names,
types, defaults, which are secrets), the CLI commands and HTTP endpoints, error
handling, and what is explicitly out of scope. List open questions for me,
each with your recommendation.
Don't write code yet.
```

After you approve the design:

```text
The design in docs/design/0001-initial-design.md is approved (with these
changes: <...>). Implement it in small steps, each a separate commit with a
Conventional Commit message: settings first, then the core logic with tests,
then the CLI/API surface. After each step, run the Definition of done commands
from AGENTS.md and fix failures without weakening any check. Then replace
every <!-- guidance --> comment in docs/wiki/ with real content for what now
exists, and regenerate the generated sections. Finish with a summary: what was
built, what's untested, what's left, and every assumption you made.
```

## 2. A feature

```text
Read AGENTS.md. Feature: <description>. Acceptance: <observable behaviour>.
First tell me which files and wiki pages you expect to change, and whether
this needs a design record (AGENTS.md "How to work here"). Wait for my go-ahead.
```

## 3. A bug fix

```text
Read AGENTS.md. Bug: <what happens, what should happen, logs/steps>.
First write a failing test that reproduces it and show me it failing. Then fix
it with the smallest change, and add a Troubleshooting entry if users could
hit it. Explain the root cause in the PR description.
```

## 4. Fill in or audit the wiki

```text
Read AGENTS.md "Docs map" and docs/wiki/.standard.toml. Compare every page in
docs/wiki/ with the code as it is now. Replace <!-- guidance --> comments with
real content, correct anything out of date, and add Troubleshooting and FAQ
entries for real failure modes you find in the code. Don't touch generated
sections: run the generator. Don't invent features. Where the code doesn't
tell you something (an SLA, a support contact), leave a TODO(wiki) marker and
list them for me. Then run python3 scripts/check-wiki.py --strict.
```

## 5. Review a PR (assistant as a second reviewer)

```text
Review the diff of this branch against main using docs/ai/review-checklist.md.
Report only concrete problems, each with file:line and why it matters, most
severe first. Then check the docs map in AGENTS.md: is every wiki page that
should have changed actually changed?
```

## Context for a chat assistant

For ChatGPT, Claude.ai or another chat with no access to the repository, paste this first, then paste
AGENTS.md and the files you're asking about:

```text
You're helping on sluice: Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores.
It's Python >= 3.13 with uv, ruff, mypy --strict and pytest.
I'll paste AGENTS.md (our rules for AI assistants) and the relevant files.
Follow AGENTS.md exactly. Give complete files or unified diffs, not fragments.
Don't assume files or APIs you haven't seen: ask for them. Every change needs
a test and a matching docs/wiki/ update.
```
