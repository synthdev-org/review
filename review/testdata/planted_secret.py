# review/testdata/planted_secret.py — a fixture file for the static-analysis
# integration tests (review/test_static_analysis_step.py): a small committed
# file with a known planted secret (for Gitleaks), deterministic because both
# the ruleset and the fixtures are pinned.
#
# The value below is an obviously-fake, clearly-marked placeholder (a
# sequential, non-random character run) shaped to match Gitleaks' own
# default "github-pat"/"generic-api-key" rules deterministically — never a
# real credential. Deliberately kept out of any path a future
# Gitleaks-on-this-repo's-own-CI pre-commit hook would scan (this repo has
# none today; noted so this fixture doesn't become a false positive if one
# is ever added later).
github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
