#!/usr/bin/env bash
# Gitleaks over the eval corpus and every cassette / fixture directory, so a
# recorded secret or a hand-written fixture secret fails CI. With no args the
# directories are discovered; pass directories to scan exactly those.
#
# Deliberately excluded: the fixtures that plant a secret on purpose (the
# planted_* files directly in review/testdata) so the Gitleaks adapter tests
# have something to detect. The other files directly in review/testdata are
# still scanned: the directory is excluded from the recursive find below
# (its subdirectories are found on their own) and its non-planted root files
# are copied to a scratch directory and scanned once.
set -euo pipefail
cd "$(dirname "$0")/.."
root_testdata="${SCAN_FIXTURES_ROOT_TESTDATA:-./review/testdata}"

if [ "$#" -gt 0 ]; then
    dirs=("$@")
else
    mapfile -t dirs < <(
        {
            echo ./review/testdata/eval
            find . -type d \( -name cassettes -o -name testdata \) \
                -not -path '*/.venv/*' -not -path '*/node_modules/*' \
                -not -path ./review/testdata -not -path ./adapter/analyzers/testdata
        } | sort -u
    )
fi

status=0
if [ "$#" -eq 0 ]; then
    scratch="$(mktemp -d)"
    trap 'rm -rf "$scratch"' EXIT
    find "$root_testdata" -maxdepth 1 -type f -not -name 'planted_*' -exec cp {} "$scratch"/ \;
    dirs+=("$scratch")
fi
for dir in "${dirs[@]}"; do
    echo "gitleaks: scanning $dir"
    gitleaks detect --no-git --source "$dir" --redact --exit-code 1 || status=1
done
exit "$status"
