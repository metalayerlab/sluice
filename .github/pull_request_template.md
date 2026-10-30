<!--
Title: a Conventional Commit, e.g. "feat: add retry to the uploader". It becomes
the squash commit on main and a CHANGELOG.md line.
-->

## What and why

<!-- What changes, and why. Link the issue or design record (docs/design/). -->

## Lessons

<!-- What let this happen, and what should change so it can't again: a test, a check,
AGENTS.md, the wiki, or project-template? Link the issue or PR. "None" is fine. -->

## AI involvement

- [ ] None
- [ ] Assisted: AI suggested parts, a human wrote or rewrote them
- [ ] Generated: most of this was written by an AI assistant (tool and model: )

## Author checklist

- [ ] Tests cover the change, and fail without it
- [ ] `docs/wiki/` is updated (see AGENTS.md "Docs map"), or this has no user-visible effect
- [ ] Generated wiki sections regenerated (`uv run scripts/gen-wiki.py`)
- [ ] No secrets, personal data or real hostnames/IPs in code, tests or docs
- [ ] New dependencies are justified below, and actually exist on the index
- [ ] Template problem found? Linked project-template issue: 

## Reviewer checklist

Human review is required for every PR, and is the whole point for generated ones:
docs/ai/review-checklist.md.

- [ ] I read every line of the diff, not only the summary
- [ ] Behaviour matches the description; edge cases and errors are handled
- [ ] Tests assert real behaviour (not just that the code runs)
