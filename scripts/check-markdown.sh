#!/usr/bin/env bash
# Template-owned: template-python. Lint the Markdown: what stack-ci.yml's Markdown lint job
# runs, with the same image and arguments (it calls this script). Without Docker it skips
# locally, but is an error in CI. Quiet on a pass: one summary line; the tool's output in
# full on a failure. Config: .markdownlint-cli2.yaml (CHANGELOG.md is release-please's).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker > /dev/null 2>&1; then
    if [ "${CI:-}" = "true" ]; then
        echo "error: docker is not installed"
        exit 1
    fi
    echo "skip: docker is not installed"
    exit 0
fi

if out="$(docker run --rm -v "$PWD:/workdir" davidanson/markdownlint-cli2:v0.24.0 \
        "docs/wiki/*.md" "*.md" "!CHANGELOG.md" 2>&1)"; then
    echo "check-markdown OK"
else
    printf 'error: markdownlint\n%s\n' "$out"
    exit 1
fi
