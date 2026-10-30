#!/usr/bin/env bash
# Lint the Markdown and scan for secrets: what CI's Wiki standard and Secret
# scan jobs run, with the same images and arguments (they call this script).
# Template-owned. Without Docker it skips locally, but is an error in CI.
# Quiet on a pass: one summary line; a failing tool's output in full (design record 0007).
#
#   scripts/check-docs-secrets.sh            # both
#   scripts/check-docs-secrets.sh docs       # markdownlint-cli2 only
#   scripts/check-docs-secrets.sh secrets    # gitleaks only
set -euo pipefail
cd "$(dirname "$0")/.."

checks="${1:-docs secrets}"

if ! command -v docker > /dev/null 2>&1; then
    if [ "${CI:-}" = "true" ]; then
        echo "error: docker is not installed"
        exit 1
    fi
    echo "skip: docker is not installed"
    exit 0
fi

status=0
passed=""

# Runs a check, printing its output only if it fails.
run() {
    local name="$1" out
    shift
    if out="$("$@" 2>&1)"; then
        passed="$passed $name"
    else
        printf 'error: %s\n%s\n' "$name" "$out"
        status=1
    fi
}

for check in $checks; do
    case "$check" in
        docs)
            # Config: .markdownlint-cli2.yaml (CHANGELOG.md is release-please's).
            run markdownlint docker run --rm -v "$PWD:/workdir" davidanson/markdownlint-cli2:v0.24.0 \
                "docs/wiki/*.md" "*.md" "!CHANGELOG.md"
            ;;
        secrets)
            # Scans commits, as CI does. Allow-list false positives in .gitleaks.toml.
            run gitleaks docker run --rm -v "$PWD:/repo" -w /repo ghcr.io/gitleaks/gitleaks:v8.30.1 \
                git --redact --verbose --no-banner --no-color --config .gitleaks.toml .
            ;;
        *)
            echo "usage: $0 [docs|secrets]"
            exit 2
            ;;
    esac
done
if [ "$status" -eq 0 ]; then
    echo "check-docs-secrets OK:$passed"
fi
exit "$status"
