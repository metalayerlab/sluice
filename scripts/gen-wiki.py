#!/usr/bin/env python3
"""Regenerate the generated sections of docs/wiki/ from the code. Template-owned: template-python.

    uv run scripts/gen-wiki.py           # rewrite the sections
    uv run scripts/gen-wiki.py --check   # CI: fail if any of them is stale

A generated section sits between markers, and everything between them is replaced:

    <!-- generated:start settings -->
    ...
    <!-- generated:end settings -->

Blocks: settings (config.SETTINGS), cli (the Typer app), http-api (the OpenAPI schema).
Write prose around the markers, never inside them.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

WIKI = Path("docs/wiki")


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def settings() -> str:
    from sluice.config import SETTINGS

    rows = [
        "| Key | Environment variable | Default | Description |",
        "|---|---|---|---|",
    ]
    for s in SETTINGS:
        # A config.py from before list settings (its src/ is the project's) has no is_list, and
        # one on interpose before v0.4.0 has no is_section.
        is_list = bool(getattr(s, "is_list", False))
        if getattr(s, "is_section", False):
            # A section (a YAML mapping, or a list of them) is read from its _FILE (a YAML file)
            # or the config file, never a plain variable; its default is always empty.
            kind = "A list of sections" if is_list else "A section"
            default = "*(empty list)*" if is_list else "*(empty)*"
            note = f" {kind}: YAML, from the config file or the `_FILE` variable's file."
            variables = f"`{s.env}_FILE`"
            # A list section with item fields (interpose v0.4.1) also reads indexed variables.
            fields: tuple[object, ...] = getattr(s, "fields", ())
            if is_list and fields:
                variables += f", or `{s.env}_<N>_<FIELD>`"
                note += " Or item N's fields as indexed variables, N from 0, each with a `_FILE`."
            rows.append(f"| `{s.key}` | {variables} | {default} | {_cell(s.help)}{note} |")
            continue
        empty = "*(empty list)*" if is_list else "*(none)*"
        default = empty if s.default == "" else f"`{s.default}`"
        listed = " A list: YAML, or comma-separated." if is_list else ""
        secret = " 🔒 Secret: use the `_FILE` variant." if s.secret else ""
        rows.append(f"| `{s.key}` | `{s.env}` | {default} | {_cell(s.help)}{listed}{secret} |")
    return "\n".join(rows)


def cli() -> str:
    import typer

    from sluice.cli import app

    # Typer vendors click, so duck-type the command objects instead of
    # importing click's classes.
    root: Any = typer.main.get_command(app)
    out: list[str] = []
    for name, cmd in _leaves(root, ""):
        out += [f"### `sluice {name}`", "", (cmd.help or "").strip(), ""]
        arguments = [p for p in cmd.params if getattr(p, "param_type_name", "") == "argument"]
        if arguments:
            out += ["| Argument | Description |", "|---|---|"]
            for arg in arguments:
                metavar = arg.metavar or arg.name.upper()
                out.append(f"| `{metavar}` | {_cell(getattr(arg, 'help', '') or '')} |")
            out.append("")
        options = [p for p in cmd.params if getattr(p, "param_type_name", "") == "option"]
        if options:
            out += ["| Option | Environment variable | Description |", "|---|---|---|"]
            for opt in options:
                flags = ", ".join(f"`{o}`" for o in [*opt.opts, *opt.secondary_opts])
                env = f"`{opt.envvar}`" if isinstance(opt.envvar, str) else ""
                out.append(f"| {flags} | {env} | {_cell(opt.help or '')} |")
            out.append("")
    return "\n".join(out).rstrip()


def _leaves(group: Any, prefix: str) -> list[tuple[str, Any]]:
    """Every runnable command under a group, sub-apps (`app.add_typer`) included."""
    leaves = []
    for name, cmd in sorted(getattr(group, "commands", {}).items()):
        if hasattr(cmd, "commands"):
            leaves += _leaves(cmd, f"{prefix}{name} ")
        else:
            leaves.append((f"{prefix}{name}", cmd))
    return leaves


BLOCKS: dict[str, Callable[..., str]] = {
    "settings": settings,
    "cli": cli,
}


def render(text: str) -> str:
    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in BLOCKS:
            raise SystemExit(f"error: unknown generated block {name!r}")
        return f"{m.group(0).split('-->')[0]}-->\n{BLOCKS[name]()}\n<!-- generated:end {name} -->"

    pattern = re.compile(
        r"<!--\s*generated:start\s+(\S+)\s*-->.*?<!--\s*generated:end\s+\1\s*-->", re.S
    )
    return pattern.sub(replace, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a section is stale")
    args = parser.parse_args(argv)

    stale = []
    for page in sorted(WIKI.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        new = render(text)
        if new != text:
            stale.append(page)
            if not args.check:
                page.write_text(new, encoding="utf-8")
    if args.check and stale:
        for page in stale:
            print(f"error: {page}: generated section is stale")
        print("Run `uv run scripts/gen-wiki.py` and commit the result.")
        return 1
    print(("stale: " if args.check else "updated: ") + (", ".join(map(str, stale)) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
