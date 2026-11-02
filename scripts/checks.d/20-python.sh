#!/bin/sh
# Template-owned: template-python. The Python stack's cheap checks, run by scripts/checks.sh
# (and so by base-ci's `checks` job and pre-commit): the wiki standard, the version and the
# lock file. Lint, types and tests need the project installed: stack-ci.yml runs them.
# Quiet on a pass: one line.
set -eu
cd "$(dirname "$0")/../.."

# Release PRs are strict: nothing ships while a page still holds skeleton guidance.
case "${GITHUB_HEAD_REF:-}" in
    release-please--*) python3 scripts/check-wiki.py --strict ;;
    *) python3 scripts/check-wiki.py ;;
esac

VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)
MANIFEST=$(python3 -c 'import json; print(json.load(open(".release-please-manifest.json"))["."])')
echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$' \
    || { echo "error: pyproject.toml: version $VERSION is not PEP 440"; exit 1; }
[ "$VERSION" = "$MANIFEST" ] \
    || { echo "error: pyproject.toml ($VERSION) and .release-please-manifest.json ($MANIFEST) disagree: only release-please changes them"; exit 1; }
uv lock --check --quiet 2> /dev/null \
    || { echo "error: uv.lock is out of step with pyproject.toml: run uv lock"; exit 1; }
echo "20-python OK"
