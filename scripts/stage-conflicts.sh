#!/bin/sh
# Template-owned: template-base. After `copier update --conflict inline` (template-update.yml
# runs it): Copier leaves each conflicted file unmerged in git's index, which blocks any
# later checkout or commit. This stages those files as they are, <<<<<<< markers and all,
# so the update can be committed and the markers resolved in its PR. Prints each staged
# path, one per line; prints nothing when there was no conflict.
set -eu
cd "$(dirname "$0")/.."

git diff --name-only --diff-filter=U | while IFS= read -r path; do
    git add -A -- "$path"
    echo "$path"
done
