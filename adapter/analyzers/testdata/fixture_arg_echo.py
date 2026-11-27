# adapter/analyzers/testdata/fixture_arg_echo.py — a fixture command for
# adapter/analyzers/test_subprocess_runner.py's `--` end-of-options-
# separator proof: an argparse-based CLI (the same class of parser Semgrep's
# own Click-based CLI belongs to — both reject an unrecognized `-`-prefixed
# argument unless a `--` precedes it) that prints its own parsed positional
# TARGETS as JSON.
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("targets", nargs="*")
args = parser.parse_args()
print(json.dumps(args.targets))
