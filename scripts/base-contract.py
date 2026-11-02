#!/usr/bin/env python3
"""Check the stack contract between template-base and the overlays stacked on it.

Template-owned: template-base (decision 0001). Stdlib only. Base names no overlay: it
learns them from the answers files (`.copier-answers.<name>.yml`, and
`.copier-answers.stack-<name>.yml` for the one stack overlay) and from the
`Template-owned: template-<name>` marker at the top of every template-owned script and
workflow. It fails when:

  - there isn't exactly one stack overlay's answers file, or base's is missing;
  - a slot is missing (base expects it by a fixed name; the stack overlay fills it);
  - a template-owned file names a template with no answers file, or a workflow or
    drop-in doesn't carry its owner's prefix;
  - a composite file the stack overlay owns lacks an entry base depends on;
  - the files every AI assistant session loads are over their context budget.

    python3 scripts/base-contract.py   (scripts/checks.d/00-base.sh runs it)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Fixed names base expects and the stack overlay fills.
SLOTS = {
    ".github/workflows/stack-ci.yml": "on:\n  workflow_call",
    ".github/dependabot.yml": "package-ecosystem: github-actions",
    ".agents/stack.md": "",
    ".gitignore": "",
    ".pre-commit-config.yaml": "",
    ".claude/settings.json": "",
    "README.md": "",
    ".github/pull_request_template.md": "Template problem found?",
    ".github/ISSUE_TEMPLATE/config.yml": "/security/advisories/new",
}

# Entries base's rules depend on, in the composite files a tool can't split.
GITIGNORE = [".env", ".env.*", "secrets/", "*.pem", "*.key", "private/",
             ".claude/settings.local.json", "CLAUDE.local.md",
             ".graphify/", "graphify-out/"]  # fmt: skip
PRE_COMMIT = ["id: gitleaks"]
ALLOW = ["Bash(gh:*)"]
DENY = [
    "Edit(.copier-answers.*.yml)",
    "Bash(git push * main)", "Bash(git push * main *)",
    "Bash(git push *:main)", "Bash(git push *:main *)",
    "Bash(git push *refs/heads/main)", "Bash(git push *refs/heads/main *)",
    "Bash(git push *--force*)", "Bash(git push * -f*)",
    "Bash(git push *+*)", "Bash(git push *--mirror*)",
    "Bash(git push *--tags*)", "Bash(git push *--follow-tags*)", "Bash(git tag:*)",
    "Bash(gh release:*)", "Bash(gh api *releases*)",
    "Bash(gh repo delete:*)", "Bash(gh repo archive:*)",
    "Bash(gh pr create *-R *)", "Bash(gh pr create *--repo*)",
    "Read(./.graphify/**)", "Read(./graphify-out/**)",
    "Read(.env)", "Read(.env.*)", "Read(secrets/**)",
]  # fmt: skip
HOOK = "scripts/repo-boundary.py"

# Characters (~4 per token). AGENTS.md and .agents/stack.md are loaded together, so they
# share AGENTS.md's budget. Raising one is a change to template-base, never local.
BUDGET = {
    ("AGENTS.md", ".agents/stack.md"): 9000,
    ("CLAUDE.md",): 600,
    ("GEMINI.md",): 300,
    (".cursor/rules/project.mdc",): 300,
    (".github/copilot-instructions.md",): 700,
}

MARKER = re.compile(r"Template-owned: template-([a-z0-9][a-z0-9-]*)")
ANSWERS = re.compile(r"^\.copier-answers\.(stack-)?([a-z0-9][a-z0-9-]*)\.yml$")
DROP_INS = ["scripts/checks.d", "scripts/post-update.d", "scripts/bootstrap.d"]


def owners(root: Path, errors: list[str]) -> dict[str, bool]:
    """Template name -> is it the stack overlay, from the answers files."""
    found: dict[str, bool] = {}
    for path in sorted(root.glob(".copier-answers.*.yml")):
        if match := ANSWERS.match(path.name):
            found[match.group(2)] = bool(match.group(1))
    stacks = sorted(name for name, stack in found.items() if stack)
    if "base" not in found or found["base"]:
        errors.append("error: .copier-answers.base.yml is missing: apply template-base first")
    if len(stacks) != 1:
        have = ", ".join(f".copier-answers.stack-{s}.yml" for s in stacks) or "none"
        errors.append(
            "error: exactly one stack overlay's answers file (.copier-answers.stack-*.yml)"
            f" is needed; found: {have}"
        )
    return found


def owned_files(root: Path) -> list[Path]:
    paths = [p for p in (root / ".github/workflows").glob("*") if p.is_file()]
    paths += [
        p for p in (root / "scripts").rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ]
    return sorted(paths)


def check_owners(root: Path, found: dict[str, bool], errors: list[str]) -> None:
    for path in owned_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12])
        except (UnicodeDecodeError, OSError):
            continue
        match = MARKER.search(head)
        if not match:
            continue  # the project's own file
        owner = match.group(1)
        if owner not in found:
            errors.append(
                f"error: {rel}: owned by template-{owner},"
                f" which has no answers file (.copier-answers.*{owner}.yml)"
            )
            continue
        if owner == "base" or rel in SLOTS:
            continue
        name = path.name
        if any(rel.startswith(d + "/") for d in DROP_INS):
            name = re.sub(r"^\d+-", "", name)
            prefixed = name.startswith(owner + "-") or name == owner + ".sh"
        elif rel.startswith(".github/workflows/"):
            prefixed = name.startswith(owner + "-")
        else:
            prefixed = True  # a script: the marker is enough
        if not prefixed:
            errors.append(
                f"error: {rel}: template-{owner}'s file must be named {owner}-* (ownership by name)"
            )


def check_slots(root: Path, errors: list[str]) -> None:
    for slot, needs in SLOTS.items():
        path = root / slot
        if not path.is_file():
            errors.append(f"error: {slot} is missing: it's a slot the stack overlay fills")
        elif needs and needs not in path.read_text(encoding="utf-8"):
            errors.append(f"error: {slot} lacks {needs!r}")


def check_composites(root: Path, errors: list[str]) -> None:
    if (path := root / ".gitignore").is_file():
        lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
        if missing := [e for e in GITIGNORE if e not in lines]:
            errors.append(f"error: .gitignore lacks base's entries: {', '.join(missing)}")
    if (path := root / ".pre-commit-config.yaml").is_file():
        text = path.read_text(encoding="utf-8")
        if missing := [e for e in PRE_COMMIT if e not in text]:
            errors.append(f"error: .pre-commit-config.yaml lacks the {', '.join(missing)} hook")
    if (path := root / ".claude/settings.json").is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"error: .claude/settings.json is not valid JSON: {e}")
            return
        permissions = settings.get("permissions", {})
        if missing := [e for e in ALLOW if e not in permissions.get("allow", [])]:
            errors.append(f"error: .claude/settings.json allow lacks: {', '.join(missing)}")
        if missing := [e for e in DENY if e not in permissions.get("deny", [])]:
            errors.append(f"error: .claude/settings.json deny lacks: {', '.join(missing)}")
        if HOOK not in json.dumps(settings.get("hooks", {})):
            errors.append(f"error: .claude/settings.json lacks the {HOOK} PreToolUse hook")


def check_budget(root: Path, errors: list[str]) -> None:
    for names, limit in BUDGET.items():
        size = sum(len(p.read_text(encoding="utf-8")) for n in names if (p := root / n).is_file())
        if size > limit:
            errors.append(
                f"error: {' + '.join(names)}: {size} characters,"
                f" over the context budget of {limit}: keep rules terse"
            )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    found = owners(root, errors)
    check_slots(root, errors)
    check_owners(root, found, errors)
    check_composites(root, errors)
    check_budget(root, errors)
    if errors:
        print("\n".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
