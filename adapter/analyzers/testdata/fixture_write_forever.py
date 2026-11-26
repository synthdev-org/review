# adapter/analyzers/testdata/fixture_write_forever.py — a fixture command
# for adapter/analyzers/test_subprocess_runner.py's bounded-read proof:
# writes chunks to stdout forever, so the test can assert the output-size
# cap actually kills a still-running process mid-read, not merely that it
# would eventually stop on its own once it finished.
import sys

while True:
    sys.stdout.write("x" * 8192)
    sys.stdout.flush()
