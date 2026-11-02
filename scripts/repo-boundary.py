#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keep file edits inside this repository.

Template-owned: template-base; changes come through `copier update` (its decision 0001).
Stdlib only (Python >= 3.11) plus git, so it runs in any stack's project.

Another codebase is changed by an issue in its own repository, never from here (AGENTS.md).
So an Edit, Write or NotebookEdit is denied when its path belongs to another repository (a
submodule, a clone, a sibling checkout) or to none. This repository's worktrees, the temp
directory and Claude Code's own files (~/.claude: memory, plans) stay writable. It catches
mistakes; it is not a security boundary, and Bash is not checked.

    echo '{"tool_input": {"file_path": "../x"}}' | scripts/repo-boundary.py   # exit 2: denied
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ADVICE = (
    "Change another codebase by an issue in its own repository (search first, then draft it "
    "for the user to confirm), or in a session opened there."
)


def common_dir(directory: Path) -> Path | None:
    """The git directory shared by all worktrees of the repository holding directory, or None."""
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def blocked(path: Path, root: Path) -> str | None:
    """Why editing path from the project at root is not allowed, or None."""
    here = common_dir(root)
    existing = next(p for p in (path, *path.parents) if p.is_dir())
    if here is not None and common_dir(existing) == here:
        return None
    if path.is_relative_to(root):
        if here is None:  # not a git repository yet: the project directory is the boundary
            return None
        return f"{path} is in another repository (a submodule or a clone). {ADVICE}"
    tmp = Path("/tmp")  # noqa: S108 - a place edits may go, not a file this script creates
    scratch = [Path(tempfile.gettempdir()).resolve(), tmp.resolve(), Path.home() / ".claude"]
    if any(path.is_relative_to(s) for s in scratch):
        return None
    return f"{path} is outside this repository. {ADVICE}"


def main() -> int:
    event = json.load(sys.stdin)
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return 0
    cwd = Path(event.get("cwd") or os.getcwd())
    path = (cwd / Path(target).expanduser()).resolve()
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd).resolve()
    reason = blocked(path, root)
    if reason:
        print(reason, file=sys.stderr)  # exit 2: Claude Code blocks the call and shows this
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
