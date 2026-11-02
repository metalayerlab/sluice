#!/bin/sh
# Template-owned: template-base. After `copier update` (template-update.yml runs it, or
# you do): every template's scripts/post-update.d/*.sh, in name order (a lock refresh, say).
# A step may assume sh, python3, git and uv, and in CI the runner image's tools
# (unpinned, and maybe missing locally). A missing tool fails, naming it.
set -eu
cd "$(dirname "$0")/.."

for step in scripts/post-update.d/*.sh; do
    [ -e "$step" ] || continue
    sh "$step"
done
echo "post-update done"
