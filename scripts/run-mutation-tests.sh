#!/usr/bin/env bash
# Runs mutmut (Python mutation testing) against this repo's unit test suite.
#
# mutmut defaults its worker pool (`--max-children`) to `os.cpu_count()` —
# every core, no cap — so run raw on the host it can stall the machine. This
# script guards against that in two parts: a Docker CPU quota (`--cpus`) *and*
# the tool's own worker flag (`--max-children`) — both capped to at most 1/4
# of the host's processors, so mutmut's own process pool can't oversubscribe
# past what the container is allowed to use even if one guard is bypassed.
#
# Never run `mutmut` directly on the host, and never raise the cap without
# also raising both the Docker --cpus quota and --max-children together.
#
# Usage: ./scripts/run-mutation-tests.sh [mutmut run args]
#   ./scripts/run-mutation-tests.sh                  # full run
#   ./scripts/run-mutation-tests.sh apps.webhook_api.settings.x_1  # re-run one mutant
# Results land in ./mutants/ (gitignored) — inspect with:
#   docker run --rm -v "$PWD/mutants":/app/mutants review-dev:mutmut uv run mutmut results
#   docker run --rm -v "$PWD/mutants":/app/mutants review-dev:mutmut uv run mutmut show <mutant-name>

# No `-e`: the mutmut run itself is allowed to exit non-zero (surviving
# mutants are a normal, expected result, not a script failure) — the
# ownership fix-up below must still run either way, and the script's own
# exit code is set explicitly from mutmut's at the end.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

cpus=$(( $(nproc) / 4 ))
cpus=$(( cpus < 1 ? 1 : cpus ))
echo "Capping mutmut to ${cpus} worker(s) (1/4 of $(nproc) host CPUs), both via Docker --cpus and --max-children."

docker build --target dev -t review-dev:mutmut .

mkdir -p mutants

# .github is excluded from the Docker build context by .dockerignore (kept
# out of runtime/dev images on purpose — see its own comment there), so it
# never reaches /app for mutmut's also_copy (pyproject.toml [tool.mutmut])
# to find on its own; bind-mounted in read-only here for the same reason
# apps/test_github_workflows.py needs it at all.
docker run --rm --cpus="${cpus}" \
    -v "$PWD/mutants:/app/mutants" \
    -v "$PWD/.github:/app/.github:ro" \
    review-dev:mutmut \
    uv run mutmut run --max-children="${cpus}" "$@"
status=$?

# mutmut writes into the bind-mounted ./mutants as the container's default
# (root) user, since running as the host UID instead breaks uv's own
# HOME/cache-directory assumptions. Hand ownership back to the invoking host
# user so ./mutants can be inspected/cleaned up afterwards without sudo or
# another container.
docker run --rm -v "$PWD/mutants:/app/mutants" review-dev:mutmut \
    chown -R "$(id -u):$(id -g)" /app/mutants

exit "${status}"
