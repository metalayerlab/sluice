#!/bin/sh
# Template-owned: template-base. One-off setup after `copier copy` (Copier runs it
# with --trust). Safe to re-run. Each template adds its steps as scripts/bootstrap.d/*.
# Local only: a step may assume sh and git. For any other tool it checks first and,
# when it's missing, prints what to install and carries on, as pre-commit below does.
set -eu
cd "$(dirname "$0")/.."

[ -d .git ] || git init -q -b main
for step in scripts/bootstrap.d/*.sh; do
    [ -e "$step" ] || continue
    sh "$step"
done

if command -v pre-commit >/dev/null 2>&1; then
    pre-commit install --install-hooks -t pre-commit -t commit-msg > /dev/null
else
    echo "tip: install pre-commit (uv tool install pre-commit) and run scripts/bootstrap.sh again for local checks"
fi
echo "bootstrap done"
