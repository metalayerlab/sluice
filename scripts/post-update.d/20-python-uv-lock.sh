#!/bin/sh
# Template-owned: template-python. After `copier update` (scripts/post-update.sh runs it, and
# template-update.yml runs that): refresh uv.lock for a changed pyproject.toml, and the wiki's
# generated sections for changed code.
set -eu
cd "$(dirname "$0")/../.."

uv lock --quiet
uv run --quiet scripts/gen-wiki.py > /dev/null
