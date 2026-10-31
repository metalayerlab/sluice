#!/usr/bin/env python3
"""Keep GitHub's About box (description, website, topics) in step with .github/repository.toml.

Template-owned: changes come from project-template through `copier update` (its design
record 0010). Stdlib only (Python >= 3.11) plus the GitHub CLI, so any layer's CI can run it.

    scripts/repo-metadata.py             # apply the file with your `gh` login (repo admin)
    scripts/repo-metadata.py --check     # compare the file with GitHub; exit 1 on a difference
    scripts/repo-metadata.py --validate  # the file only, offline

Exit 2: the file is invalid. Topics are set to exactly the file's list, so topics added in
the web UI and not in the file are removed (`--check` lists them first).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

FILE = Path(".github/repository.toml")
TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")  # GitHub's rule
MAX_TOPICS = 20
KEYS = ("description", "homepage", "topics")


def load(path: Path) -> tuple[dict[str, object], list[str]]:
    """The file's values, and what's wrong with them."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {}, [f"cannot read {path}: {e}"]
    problems = [f"unknown key {k!r}" for k in data if k not in KEYS]
    for key in ("description", "homepage"):
        if not isinstance(data.get(key, ""), str):
            problems.append(f"{key} must be a string")
    homepage = data.get("homepage", "")
    if isinstance(homepage, str) and homepage and not homepage.startswith("https://"):
        problems.append("homepage must start with https://")
    topics = data.get("topics", [])
    if not isinstance(topics, list):
        return data, [*problems, "topics must be a list"]
    problems += [f"topic {t!r}: lowercase letters, digits and hyphens, at most 50" for t in topics
                 if not isinstance(t, str) or not TOPIC.match(t)]  # fmt: skip
    if len(topics) > MAX_TOPICS:
        problems.append(f"{len(topics)} topics: GitHub allows {MAX_TOPICS}")
    if len(set(map(str, topics))) != len(topics):
        problems.append("duplicate topics")
    return data, problems


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def differences(want: dict[str, object]) -> list[str]:
    live = json.loads(gh("api", "repos/{owner}/{repo}"))
    out = []
    for key in ("description", "homepage"):
        if (live.get(key) or "") != want.get(key, ""):
            out.append(f"{key}: GitHub has {live.get(key) or ''!r}, the file {want.get(key, '')!r}")
    have, need = set(live.get("topics") or []), set(topic_list(want))
    if missing := sorted(need - have):
        out.append(f"topics missing on GitHub: {', '.join(missing)}")
    if extra := sorted(have - need):
        out.append(f"topics on GitHub, not in the file (applying removes them): {', '.join(extra)}")
    return out


def topic_list(want: dict[str, object]) -> list[str]:
    return [str(t) for t in want.get("topics", [])]  # type: ignore[attr-defined]


def apply(want: dict[str, object]) -> None:
    gh("repo", "edit", "--enable-wiki", "--description", str(want.get("description", "")),
       "--homepage", str(want.get("homepage", "")))  # fmt: skip
    body = json.dumps({"names": topic_list(want)})
    gh("api", "-X", "PUT", "repos/{owner}/{repo}/topics", "--input", "-", stdin=body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare with GitHub, change nothing")
    mode.add_argument("--validate", action="store_true", help="check the file only, offline")
    parser.add_argument("--file", type=Path, default=FILE)
    args = parser.parse_args(argv)

    want, problems = load(args.file)
    for problem in problems:
        print(f"error: {args.file}: {problem}")
    if problems:
        return 2
    if args.validate:
        return 0
    try:
        if args.check:
            diffs = differences(want)
            for diff in diffs:
                print(diff)
            return 1 if diffs else 0
        apply(want)
    except RuntimeError as e:
        print(f"error: {e}")
        return 1
    print(f"GitHub's About box now matches {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
