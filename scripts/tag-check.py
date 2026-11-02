#!/usr/bin/env python3
"""Bump dependencies pinned to a GitHub tag, which Dependabot can't (template-python's 0002).

Template-owned: template-python. Changes come from there through `copier update`.
Stdlib only (Python >= 3.11): python-tag-check.yml runs it weekly, before anything is installed.

    scripts/tag-check.py --dry-run                     # what it would bump (tags from GitHub)
    scripts/tag-check.py --dry-run --tags-from t.json  # {"OWNER/REPO": ["v0.1.0", ...]}
    scripts/tag-check.py                               # push tag-check/NAME, open or edit its PR

It reads pyproject.toml's `NAME @ git+https://github.com/OWNER/REPO@vX.Y.Z` requirements and
[tool.uv.sources] `{ git = "https://github.com/OWNER/REPO", tag = "vX.Y.Z" }` entries. A branch,
commit or pre-release pin is reported and skipped. For the highest newer vX.Y.Z tag it rewrites
only the pin's line, runs `uv lock --upgrade-package NAME`, and opens one PR per dependency.

Environment: GH_TOKEN or GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_API_URL. RELEASE_APP_CLIENT_ID
unset means the token is GITHUB_TOKEN, whose PRs start no workflows: the PR body says so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEMVER = re.compile(r"(v?)(\d+)\.(\d+)\.(\d+)")
PRERELEASE = re.compile(r"v?\d+\.\d+\.\d+[-+.]?[0-9A-Za-z][0-9A-Za-z.+-]*")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
# NAME[extras] @ git+https://github.com/OWNER/REPO[.git][@REF][; markers]
DIRECT = re.compile(
    r"\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*@\s*"
    r"git\+https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:@(?P<ref>[^\s;#]+))?\s*(?:;.*)?"
)
GITHUB = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?")
SQUAT = "Dependabot looks this name up on PyPI: treat a Dependabot PR for it as suspect."
Version = tuple[int, int, int]


@dataclass
class Pin:
    name: str
    owner: str
    repo: str
    ref: str
    form: str  # "direct" (PEP 508) or "sources" ([tool.uv.sources])
    skip: str = ""  # why it isn't checked: a branch, a commit, a pre-release

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        """The normalised name (PEP 503): the branch's, and uv's --upgrade-package."""
        return re.sub(r"[-_.]+", "-", self.name).lower()


@dataclass
class Bump:
    pin: Pin
    new: str
    breaking: str = ""  # "major", "0.x minor" or ""
    lines: list[int] = field(default_factory=list)

    @property
    def message(self) -> str:
        return f"chore(deps): bump {self.pin.name} from {self.pin.ref} to {self.new}"

    @property
    def title(self) -> str:
        return self.message + (" (breaking)" if self.breaking else "")


def version(tag: str) -> Version | None:
    """(major, minor, patch) of a strict vX.Y.Z or X.Y.Z tag; None for anything else."""
    m = SEMVER.fullmatch(tag)
    return (int(m[2]), int(m[3]), int(m[4])) if m else None


def classify(ref: str | None, kind: str) -> str:
    """Why a ref isn't a checkable vX.Y.Z tag, or "" when it is. kind: the uv source's key
    (tag, branch, rev), or "ref" for a PEP 508 `@REF`, which may be any of them."""
    if not ref:
        return "not pinned"
    if version(ref):
        return ""
    if kind == "branch":
        return f"branch pin ({ref})"
    if kind == "rev" or COMMIT.fullmatch(ref):
        return f"commit pin ({ref})"
    if PRERELEASE.fullmatch(ref):
        return f"pre-release pin ({ref})"
    return f"branch pin ({ref})" if kind == "ref" else f"tag {ref} isn't vX.Y.Z"


def requirements(data: dict[str, Any]) -> list[str]:
    """[project].dependencies, [project.optional-dependencies] and [dependency-groups]."""
    project = data.get("project", {})
    found = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        found += group
    for group in data.get("dependency-groups", {}).values():
        found += [r for r in group if isinstance(r, str)]  # not {include-group = ...}
    return found


def find_pins(data: dict[str, Any]) -> list[Pin]:
    """Every GitHub git dependency, once per form; a pin's `skip` says why it isn't checked."""
    pins: dict[tuple[str, str], Pin] = {}
    for req in requirements(data):
        m = DIRECT.fullmatch(req)
        if m:
            ref = m["ref"] or ""
            pin = Pin(m["name"], m["owner"], m["repo"], ref, "direct", classify(ref, "ref"))
            pins.setdefault((pin.key, pin.form), pin)
    for name, source in data.get("tool", {}).get("uv", {}).get("sources", {}).items():
        for entry in source if isinstance(source, list) else [source]:
            g = GITHUB.fullmatch(str(entry.get("git", "")))
            if not g:
                continue
            kind = next((k for k in ("tag", "branch", "rev") if k in entry), "tag")
            ref = str(entry.get(kind, ""))
            pin = Pin(name, g["owner"], g["repo"], ref, "sources", classify(ref, kind))
            pins.setdefault((pin.key, pin.form), pin)
    return list(pins.values())


def choose(pin: Pin, tags: list[str]) -> Bump | None:
    """The highest strict-semver tag above the pin, or None. Where a version is tagged both
    with and without a `v`, the pin's style wins; the tag's own name is what's written."""
    current = version(pin.ref)
    if current is None:
        raise ValueError(f"{pin.name}: {pin.ref} isn't vX.Y.Z")
    style = pin.ref.startswith("v")
    best: tuple[Version, bool, str] | None = None
    for tag in tags:
        v = version(tag)
        if v is not None and v > current:
            candidate = (v, tag.startswith("v") == style, tag)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None
    new = best[0]
    breaking = "major" if new[0] > current[0] else ""
    if current[0] == 0 and new[0] == 0 and new[1] > current[1]:
        breaking = "0.x minor"
    return Bump(pin, best[2], breaking)


def pin_lines(text: str, pin: Pin) -> list[int]:
    """The lines of pyproject.toml that hold this pin: the only ones rewritten."""
    lines = text.splitlines()
    slug, ref = re.escape(pin.slug), re.escape(pin.ref)
    if pin.form == "direct":
        url = re.compile(rf"git\+https://github\.com/{slug}(?:\.git)?@{ref}(?=[\s;\"'#]|$)")
        name = re.compile(rf"[\"']\s*{re.escape(pin.name)}\s*[\[@]", re.IGNORECASE)
        return [i for i, line in enumerate(lines) if url.search(line) and name.search(line)]
    entry = re.compile(
        rf"^\s*[\"']?{re.escape(pin.name)}[\"']?\s*=.*github\.com/{slug}\b.*"
        rf"\btag\s*=\s*[\"']{ref}[\"']"
    )
    return [i for i, line in enumerate(lines) if entry.search(line)]


def rewrite(text: str, bump: Bump) -> str:
    """pyproject.toml with the pin's own lines moved to the new tag; nothing else changes."""
    lines = text.splitlines(keepends=True)
    old = re.escape(bump.pin.ref)
    if bump.pin.form == "direct":
        pattern = rf"(@){old}(?=[\s;\"'#]|$)"
    else:
        pattern = rf"(\btag\s*=\s*[\"']){old}(?=[\"'])"
    for i in bump.lines:
        lines[i] = re.sub(pattern, lambda m: m[1] + bump.new, lines[i], count=1)
    return "".join(lines)


class GitHub:
    def __init__(self, token: str, api: str, repository: str) -> None:
        self.token, self.api, self.repository = token, api.rstrip("/"), repository

    def call(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        body = json.dumps(data).encode() if data is not None else None
        # The API URL is GitHub's (GITHUB_API_URL), never a file: or custom scheme.
        request = urllib.request.Request(self.api + path, data=body, method=method)  # noqa: S310
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
        return json.loads(raw) if raw else None

    def tags(self, slug: str) -> list[str]:
        names: list[str] = []
        page = 1
        while True:
            batch = self.call("GET", f"/repos/{slug}/tags?per_page=100&page={page}")
            names += [t["name"] for t in batch]
            if len(batch) < 100:
                return names
            page += 1

    def open_pr(self, branch: str) -> dict[str, Any] | None:
        owner = self.repository.split("/")[0]
        found = self.call("GET", f"/repos/{self.repository}/pulls?state=open&head={owner}:{branch}")
        return found[0] if found else None


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def pr_body(bump: Bump, app: bool) -> str:
    pin, home = bump.pin, f"https://github.com/{bump.pin.slug}"
    lines = [
        f"Bumps [{pin.name}]({home}) from `{pin.ref}` to `{bump.new}`, its highest release.",
        "",
    ]
    if bump.breaking:
        lines += [f"**Breaking:** a {bump.breaking} bump. Read the changes before merging.", ""]
    lines += [
        f"- Changes: {home}/compare/{pin.ref}...{bump.new}",
        f"- Release: {home}/releases/tag/{bump.new}",
        f"- `pyproject.toml`: the pin's line. `uv.lock`: `uv lock --upgrade-package {pin.key}`.",
        "",
    ]
    if not app:
        lines += [
            "**CI hasn't run on this PR.** It was pushed with `GITHUB_TOKEN`, whose events start no"
            " workflows, so `ci` won't report: **close and reopen this PR** to run it. The release"
            " GitHub App (`RELEASE_APP_CLIENT_ID` variable, `RELEASE_APP_PRIVATE_KEY` secret) makes"
            " that unnecessary.",
            "",
        ]
    lines.append(
        f"From `scripts/tag-check.py` (`python-tag-check.yml`). A newer tag rewrites"
        f" `tag-check/{pin.key}`; when the pin is current, this PR is closed."
    )
    return "\n".join(lines) + "\n"


def propose(bump: Bump, gh: GitHub, root: Path, base: str, app: bool) -> str:
    """Branch tag-check/NAME from origin/BASE: rewrite, lock, commit, force-push, open or edit."""
    branch = f"tag-check/{bump.pin.key}"
    git("fetch", "--quiet", "origin", base, cwd=root)
    git("checkout", "--quiet", "--force", "-B", branch, f"origin/{base}", cwd=root)
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text()
    bump.lines = pin_lines(text, bump.pin)
    if not bump.lines:
        raise ValueError(f"no line of {base}'s pyproject.toml holds the pin {bump.pin.ref}")
    pyproject.write_text(rewrite(text, bump))
    subprocess.run(["uv", "lock", "--upgrade-package", bump.pin.key], cwd=root, check=True)
    git("add", "pyproject.toml", "uv.lock", cwd=root)
    git("commit", "--quiet", "-m", bump.message, cwd=root)
    git("push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}", cwd=root)
    fields = {"title": bump.title, "body": pr_body(bump, app)}
    pr = gh.open_pr(branch)
    if pr:
        gh.call("PATCH", f"/repos/{gh.repository}/pulls/{pr['number']}", fields)
        return f"updated #{pr['number']}"
    made = gh.call(
        "POST", f"/repos/{gh.repository}/pulls", {**fields, "head": branch, "base": base}
    )
    return f"opened #{made['number']}"


def close_stale(pin: Pin, gh: GitHub) -> str:
    """The pin is current: close an open tag-check PR for it, saying why."""
    pr = gh.open_pr(f"tag-check/{pin.key}")
    if not pr:
        return "current"
    n = pr["number"]
    note = f"`{pin.ref}` is the pin on the base branch and no newer tag exists: closing."
    gh.call("POST", f"/repos/{gh.repository}/issues/{n}/comments", {"body": note})
    gh.call("PATCH", f"/repos/{gh.repository}/pulls/{n}", {"state": "closed"})
    return f"current; closed #{n}"


def check(pin: Pin, tags: list[str], text: str) -> tuple[Bump | None, str]:
    """A dry run's bump for one pin, and its report line."""
    bump = choose(pin, tags)
    if bump is None:
        return None, "current"
    bump.lines = pin_lines(text, pin)
    if not bump.lines:
        raise ValueError(f"no line of pyproject.toml holds the pin {pin.ref}")
    new = rewrite(text, bump).splitlines()
    for i in bump.lines:
        print(f"{bump.title}\n  pyproject.toml:{i + 1}: {new[i].strip()}")
    return bump, f"would bump to `{bump.new}`" + (" (breaking)" if bump.breaking else "")


def summary(text: str) -> None:
    print(text, end="")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump dependencies pinned to a GitHub tag.")
    parser.add_argument("--dry-run", action="store_true", help="report the bumps; change nothing")
    parser.add_argument("--tags-from", type=Path, help='{"OWNER/REPO": [tags]} instead of GitHub')
    parser.add_argument("--base", default="main", help="the PRs' base branch (default: main)")
    parser.add_argument("--root", type=Path, default=Path("."), help="the project (default: .)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    text = (root / "pyproject.toml").read_text()
    pins = find_pins(tomllib.loads(text))
    if not pins:
        summary("tag-check: no tag-pinned git dependencies\n")
        return 0

    gh = GitHub(
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""),
    )
    if not args.dry_run and not gh.repository:
        parser.error("GITHUB_REPOSITORY (OWNER/REPO) is needed to open PRs; or pass --dry-run")
    app = bool(os.environ.get("RELEASE_APP_CLIENT_ID"))
    fixture = json.loads(args.tags_from.read_text()) if args.tags_from else None

    report = ["## Tag check", "", "| Dependency | Pin | Result |", "|---|---|---|"]
    failed = moved = False
    for pin in pins:
        where = f"[{pin.name}](https://github.com/{pin.slug})"
        where += " in `[tool.uv.sources]`" if pin.form == "sources" else ""
        if pin.skip:
            report.append(f"| {where} | `{pin.ref or '-'}` | skipped: {pin.skip} |")
            continue
        try:
            tags = fixture.get(pin.slug, []) if fixture is not None else gh.tags(pin.slug)
        except OSError as e:  # urllib's URLError and HTTPError are OSErrors
            print(f"error: {pin.name}: can't read {pin.slug}'s tags ({e}). A private repository"
                  " needs the release App, installed on it.", file=sys.stderr)  # fmt: skip
            report.append(f"| {where} | `{pin.ref}` | **failed**: can't read its tags |")
            failed = True
            continue
        try:
            if args.dry_run:
                result = check(pin, tags, text)[1]
            elif (bump := choose(pin, tags)) is None:
                result = close_stale(pin, gh)
            else:
                moved = True
                result = f"`{bump.new}`" + (" (breaking)" if bump.breaking else "")
                result += ": " + propose(bump, gh, root, args.base, app)
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            print(f"error: {pin.name}: {e}", file=sys.stderr)
            result, failed = "**failed**: see the log", True
        report.append(f"| {where} | `{pin.ref}` | {result} |")
    report += ["", f"{SQUAT} Each of these is fetched from GitHub; close such a PR.", ""]
    summary("\n".join(report))
    if moved:
        git("checkout", "--quiet", "--force", args.base, cwd=root)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
