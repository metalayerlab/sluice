#!/bin/sh
# Template-owned: template-python. After `copier copy` (scripts/bootstrap.sh runs it): lock
# the dependencies and fill the wiki's generated sections from the code. Safe to re-run.
set -eu
cd "$(dirname "$0")/../.."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed: see https://docs.astral.sh/uv/, then re-run scripts/bootstrap.sh" >&2
    exit 1
fi
uv lock --quiet
# Fill the wiki's generated sections (settings, CLI, HTTP API) from the code.
uv run --quiet scripts/gen-wiki.py > /dev/null
