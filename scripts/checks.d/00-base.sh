#!/bin/sh
# Template-owned: template-base. Base's own checks, first in scripts/checks.sh: the
# stack contract (answers files, slots, owners by name, base entries in the composite
# files, the context budget), .github/repository.toml, and the repository-boundary hook.
# Quiet on a pass: one line.
set -eu
cd "$(dirname "$0")/../.."

python3 scripts/base-contract.py
python3 scripts/repo-metadata.py --validate > /dev/null
# The hook must deny an edit outside the repository (exit 2) and allow one inside it.
if printf '{"tool_input": {"file_path": "/nonexistent-outside/x"}}' | python3 scripts/repo-boundary.py 2> /dev/null; then
    echo "error: scripts/repo-boundary.py allowed an edit outside the repository"; exit 1
fi
printf '{"tool_input": {"file_path": "AGENTS.md"}}' | CLAUDE_PROJECT_DIR="$PWD" python3 scripts/repo-boundary.py
echo "00-base OK"
