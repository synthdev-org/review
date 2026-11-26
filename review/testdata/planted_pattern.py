# review/testdata/planted_pattern.py — a fixture file for the static-analysis
# integration tests (review/test_static_analysis_step.py): a small committed
# file with a known planted pattern (for Semgrep's pinned ruleset),
# deterministic because both the ruleset and the fixtures are pinned.
#
# Matches adapter/analyzers/semgrep_ruleset.yaml's "review-eval-usage" rule
# deterministically.


def run_untrusted(user_input: str) -> object:
    return eval(user_input)
