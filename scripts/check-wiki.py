#!/usr/bin/env python3
"""Check docs/wiki/ against the wiki standard (docs/wiki/.standard.toml).

Template-owned: changes come from project-template through `copier update`.
Stdlib only (Python >= 3.11), so any language layer's CI can run it.

    scripts/check-wiki.py                      # structure, links, sidebar
    scripts/check-wiki.py --strict             # also fail on leftover guidance (Release PRs)
    scripts/check-wiki.py --docs-touched BASE  # fail if watched paths changed since BASE
                                               # without a docs/wiki/ change

Errors, in every mode:
  - a required page is missing, or lacks one of its required headings
  - a page is not linked from _Sidebar.md
  - a link to a page or #anchor that doesn't exist, or a relative link to a
    repo path (it breaks once published: use a full URL)
  - unbalanced <!-- generated:start/end --> markers

  - a design record (docs/design/NNNN-*.md) missing from the index, docs/design/README.md
  - an always-loaded AI instruction file (AGENTS.md, CLAUDE.md, ...) over its [context_budget]

Warnings (errors with --strict): leftover <!-- guidance: ... --> comments and
TODO(wiki) markers, i.e. skeleton text that was never written.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
GUIDANCE = re.compile(r"<!--\s*guidance:|TODO\(wiki\)")
GEN_START = re.compile(r"<!--\s*generated:start\s+(\S+)\s*-->")
GEN_END = re.compile(r"<!--\s*generated:end\s+(\S+)\s*-->")
COMMENT = re.compile(r"<!--.*?-->", re.S)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def _emit(self, level: str, path: Path | None, line: int | None, msg: str) -> str:
        where = f"{path}:{line}" if path and line else f"{path}" if path else ""
        if os.environ.get("GITHUB_ACTIONS") == "true":
            loc = f" file={path}" + (f",line={line}" if line else "") if path else ""
            print(f"::{level}{loc}::{msg}")
        return f"{where}: {msg}" if where else msg

    def error(self, msg: str, path: Path | None = None, line: int | None = None) -> None:
        self.errors.append(self._emit("error", path, line, msg))

    def warning(self, msg: str, path: Path | None = None, line: int | None = None) -> None:
        self.warnings.append(self._emit("warning", path, line, msg))


def slug(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def prose_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) outside fenced code blocks, with inline code removed."""
    out: list[tuple[int, str]] = []
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            out.append((n, INLINE_CODE.sub("", line)))
    return out


def strip_comments(text: str) -> str:
    """HTML comments blanked out, keeping line numbers: links in them aren't links."""
    return COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def headings(text: str) -> list[str]:
    found = []
    for _, line in prose_lines(strip_comments(text)):
        m = HEADING.match(line)
        if m:
            found.append(m.group(2))
    return found


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    result = set()
    for h in headings(text):
        base = slug(h)
        n = seen.get(base, 0)
        result.add(base if n == 0 else f"{base}-{n}")
        seen[base] = n + 1
    return result


def norm_heading(text: str) -> str:
    """Compare headings without emoji, punctuation or case."""
    return re.sub(r"[^\w ]", "", text).strip().lower()


def check_structure(wiki: Path, standard: dict[str, Any], report: Report, strict: bool) -> None:
    pages = {p.stem: p for p in sorted(wiki.glob("*.md"))}
    texts = {name: p.read_text(encoding="utf-8") for name, p in pages.items()}

    for name, spec in standard.get("pages", {}).items():
        if name not in pages:
            report.error(f"required page {name}.md is missing", wiki)
            continue
        have = {norm_heading(h) for h in headings(texts[name])}
        for want in spec.get("headings", []):
            if norm_heading(want) not in have:
                report.error(f'missing required heading "{want}"', pages[name])

    sidebar = strip_comments(texts.get("_Sidebar", ""))
    linked = {t.split("#")[0].removesuffix(".md") for t in LINK.findall(sidebar)}
    for name in pages:
        if not name.startswith("_") and name not in linked:
            report.error(f"{name} is not linked from _Sidebar.md", pages[name])

    page_anchors = {name: anchors(text) for name, text in texts.items()}
    for name, text in texts.items():
        path = pages[name]
        for n, line in prose_lines(strip_comments(text)):
            for target in LINK.findall(line):
                check_link(name, target, page_anchors, report, path, n)
        open_blocks: list[str] = []
        for n, line in prose_lines(text):
            if GUIDANCE.search(line):
                msg = "unwritten guidance/TODO(wiki): replace it with real content"
                (report.error if strict else report.warning)(msg, path, n)
            for block in GEN_START.findall(line):
                open_blocks.append(block)
            for block in GEN_END.findall(line):
                if not open_blocks or open_blocks.pop() != block:
                    report.error(f"generated:end {block} without its start", path, n)
        for block in open_blocks:
            report.error(f"generated:start {block} is never closed", path)


def check_link(
    page: str, target: str, page_anchors: dict[str, set[str]], report: Report, path: Path, n: int
) -> None:
    if SCHEME.match(target) or target.startswith("//"):
        return
    name, _, anchor = target.partition("#")
    name = name.removesuffix(".md") or page
    if "/" in name or name.startswith("."):
        report.error(f"relative link to a repo path ({target}): use a full URL", path, n)
        return
    if name not in page_anchors:
        report.error(f"link to missing page {name}", path, n)
    elif anchor and anchor.lower() not in page_anchors[name]:
        report.error(f"link to missing anchor #{anchor} in {name}", path, n)


def check_design_index(design: Path, report: Report) -> None:
    """Every record is in the index, so assistants read the index instead of every record."""
    index = design / "README.md"
    if not index.is_file():
        return
    listed = index.read_text(encoding="utf-8")
    for record in sorted(design.glob("[0-9][0-9][0-9][0-9]-*.md")):
        number = record.name[:4]
        if number != "0000" and not re.search(rf"\|\s*\[?{number}\b", listed):
            report.error(f"design record {record.name} is not listed in the index", index)


def check_context_budget(standard: dict[str, Any], report: Report) -> None:
    """Files every assistant session loads stay small: rules in AGENTS.md, reasons in the wiki."""
    for name, limit in standard.get("context_budget", {}).items():
        path = Path(name)
        if path.is_file() and (size := len(path.read_text(encoding="utf-8"))) > limit:
            report.error(
                f"{size} characters, over the context budget of {limit}: keep rules terse and move"
                " reasons to the wiki (the budget is in docs/wiki/.standard.toml)",
                path,
            )


def check_docs_touched(wiki: Path, standard: dict[str, Any], base: str, report: Report) -> None:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True, check=True
    )
    changed = [p for p in out.stdout.splitlines() if p]
    watch = standard.get("docs_touched", {}).get("watch", [])
    hits = [p for p in changed if any(fnmatch.fnmatch(p, w) for w in watch)]
    wiki_prefix = wiki.as_posix().rstrip("/") + "/"
    if hits and not any(p.startswith(wiki_prefix) for p in changed):
        report.error(
            "user-facing code changed without a wiki change: "
            + ", ".join(hits)
            + f". Update {wiki_prefix} (see AGENTS.md, 'Docs map'), or have a"
            " reviewer add the no-docs-needed label if nothing user-visible changed."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wiki", type=Path, default=Path("docs/wiki"))
    parser.add_argument("--design", type=Path, default=Path("docs/design"))
    parser.add_argument("--strict", action="store_true", help="fail on unwritten guidance")
    parser.add_argument("--docs-touched", metavar="BASE", help="git ref to diff against")
    parser.add_argument("--verbose", action="store_true", help="list warnings, not only count them")
    args = parser.parse_args(argv)

    standard_file = args.wiki / ".standard.toml"
    try:
        standard = tomllib.loads(standard_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"error: cannot read {standard_file}: {e}", file=sys.stderr)
        return 2

    report = Report()
    if args.docs_touched:
        check_docs_touched(args.wiki, standard, args.docs_touched, report)
    else:
        check_structure(args.wiki, standard, report, args.strict)
        check_design_index(args.design, report)
        check_context_budget(standard, report)

    # Quiet on a pass: an assistant reads this output on every run (design record 0007).
    if args.verbose:
        for warning in report.warnings:
            print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}")
    hint = "" if args.verbose or not report.warnings else "; --verbose lists them"
    if report.errors:
        print(f"\n{len(report.errors)} error(s), {len(report.warnings)} warning(s){hint}")
        return 1
    print(f"wiki OK ({len(report.warnings)} warning(s){hint})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
