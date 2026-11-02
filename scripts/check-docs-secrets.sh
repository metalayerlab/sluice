#!/usr/bin/env bash
# Scan for secrets: what CI's Secret scan job runs, with the same image and
# arguments (it calls this script). A stack that lints Markdown does it in its own
# scripts/checks.d/ drop-in (template-base decision 0001).
# Template-owned: template-base. Without Docker it skips locally, but is an error in CI.
# Quiet on a pass: one summary line; a failing tool's output in full (template-base decision 0001).
#
#   scripts/check-docs-secrets.sh            # gitleaks
set -euo pipefail
cd "$(dirname "$0")/.."

checks="${1:-secrets}"

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
        secrets)
            # Scans commits, as CI does. Allow-list false positives in .gitleaks.toml.
            run gitleaks docker run --rm -v "$PWD:/repo" -w /repo ghcr.io/gitleaks/gitleaks:v8.30.1 \
                git --redact --verbose --no-banner --no-color --config .gitleaks.toml .
            ;;
        *)
            echo "usage: $0 [secrets]"
            exit 2
            ;;
    esac
done
if [ "$status" -eq 0 ]; then
    echo "check-docs-secrets OK:$passed"
fi
exit "$status"
