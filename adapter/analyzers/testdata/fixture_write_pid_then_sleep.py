# adapter/analyzers/testdata/fixture_write_pid_then_sleep.py — a fixture
# command for adapter/analyzers/test_subprocess_runner.py's kill-and-reap
# proof: writes its own PID to the given file, then sleeps far longer than
# any test's own configured timeout, so the test can assert the process was
# actually killed (not merely that the Python-side await raised).
import os
import sys
import time

with open(sys.argv[1], "w") as pid_file:
    pid_file.write(str(os.getpid()))

time.sleep(60)
