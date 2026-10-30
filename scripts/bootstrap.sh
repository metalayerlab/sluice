#!/bin/sh
# One-off setup after `copier copy` (Copier runs it for you with --trust).
# Safe to re-run: `copier update` runs it again.
set -eu
cd "$(dirname "$0")/.."

[ -d .git ] || git init -q -b main

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed: see https://docs.astral.sh/uv/, then re-run scripts/bootstrap.sh" >&2
    exit 1
fi
uv lock --quiet
# Fill the wiki's generated sections (settings, CLI, HTTP API) from the code.
uv run --quiet scripts/gen-wiki.py > /dev/null

if command -v pre-commit >/dev/null 2>&1; then
    pre-commit install --install-hooks -t pre-commit -t commit-msg > /dev/null
else
    echo "tip: install pre-commit (uv tool install pre-commit) and run scripts/bootstrap.sh again for local checks"
fi
echo "bootstrap done"
