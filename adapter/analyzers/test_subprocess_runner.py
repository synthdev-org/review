"""Tests for adapter.analyzers.subprocess_runner, per
docs/adr/0005-pure-no-io-logic-unit-tested.md: the
path-traversal guard is pure, no-I/O logic, tested at Unit tier despite
living inside ``adapter/analyzers`` — given already-fetched bytes (a fake
``dict[path, bytes]``), never a live GitHub call.

This file also holds real-subprocess Integration-tier cases
(``@pytest.mark.integration`` applied per-test, not via a module-level
``pytestmark``, since this file
now straddles both tiers, the same way ``review/test_static_analysis_step.py``
does) proving the kill-and-reap and bounded-read mechanisms themselves,
rather than only the pipeline-level isolation they enable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from adapter.analyzers.exceptions import (
    AnalyzerOutputTooLargeError,
    AnalyzerTimeoutError,
    MaterializationCollisionError,
    PathTraversalError,
)
from adapter.analyzers.subprocess_runner import (
    analyzer_env,
    materialize_files,
    run_analyzer_subprocess,
    validate_relative_path,
)

_TESTDATA_DIR = Path(__file__).parent / "testdata"

# ---------------------------------------------------------------------------
# validate_relative_path: pure guard logic
# ---------------------------------------------------------------------------


def test_validate_relative_path_accepts_a_normal_relative_path() -> None:
    assert validate_relative_path("src/main.py") == "src/main.py"


def test_validate_relative_path_accepts_a_top_level_file() -> None:
    assert validate_relative_path("README.md") == "README.md"


@pytest.mark.parametrize(
    "malicious_path",
    [
        "../etc/passwd",
        "a/../../etc/passwd",
        "src/../../secrets.env",
    ],
)
def test_validate_relative_path_rejects_dot_dot_segment(malicious_path: str) -> None:
    with pytest.raises(PathTraversalError):
        validate_relative_path(malicious_path)


@pytest.mark.parametrize("absolute_path", ["/etc/passwd", "/root/.ssh/id_rsa"])
def test_validate_relative_path_rejects_absolute_path(absolute_path: str) -> None:
    with pytest.raises(PathTraversalError):
        validate_relative_path(absolute_path)


def test_validate_relative_path_rejects_empty_path() -> None:
    with pytest.raises(PathTraversalError):
        validate_relative_path("")


def test_validate_relative_path_rejects_path_that_collapses_to_current_directory() -> None:
    with pytest.raises(PathTraversalError):
        validate_relative_path("a/..")


# ---------------------------------------------------------------------------
# validate_relative_path: a rejected path is logged at warning — a path-traversal
# attempt is itself worth an operator seeing.
# ---------------------------------------------------------------------------


def test_validate_relative_path_logs_a_warning_on_rejection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level("WARNING", logger="adapter.analyzers.subprocess_runner"),
        pytest.raises(PathTraversalError),
    ):
        validate_relative_path("../etc/passwd")

    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_validate_relative_path_warning_includes_the_rejected_filename(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level("WARNING", logger="adapter.analyzers.subprocess_runner"),
        pytest.raises(PathTraversalError),
    ):
        validate_relative_path("/etc/passwd")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].path == "/etc/passwd"  # type: ignore[attr-defined]


def test_validate_relative_path_does_not_log_when_the_path_is_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="adapter.analyzers.subprocess_runner"):
        validate_relative_path("src/main.py")

    assert caplog.records == []


# ---------------------------------------------------------------------------
# materialize_files: writes already-fetched, already-validated bytes
# ---------------------------------------------------------------------------


def test_materialize_files_writes_content_given_a_fake_dict(tmp_path: Path) -> None:
    content = {"src/main.py": b"print('hi')\n", "README.md": b"# hello\n"}

    written = materialize_files(content, tmp_path)

    assert {p.relative_to(tmp_path).as_posix() for p in written} == set(content.keys())
    assert (tmp_path / "src" / "main.py").read_bytes() == b"print('hi')\n"
    assert (tmp_path / "README.md").read_bytes() == b"# hello\n"


def test_materialize_files_rejects_a_malicious_path_and_writes_nothing_for_it(
    tmp_path: Path,
) -> None:
    content = {"../escape.py": b"evil"}

    with pytest.raises(PathTraversalError):
        materialize_files(content, tmp_path)

    assert not (tmp_path.parent / "escape.py").exists()


def test_materialize_files_logs_a_warning_on_a_rejected_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    content = {"../escape.py": b"evil"}

    with (
        caplog.at_level("WARNING", logger="adapter.analyzers.subprocess_runner"),
        pytest.raises(PathTraversalError),
    ):
        materialize_files(content, tmp_path)

    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_materialize_files_rejects_a_same_path_collision(tmp_path: Path) -> None:
    # Two distinct keys that normalize (backslash -> forward slash) to the
    # same on-disk path — the base collision case `MaterializationCollisionError`
    # exists to report via the `os.open(..., O_EXCL)` FileExistsError path.
    content = {"a/b": b"first", "a\\b": b"second"}

    with pytest.raises(MaterializationCollisionError):
        materialize_files(content, tmp_path)


def test_materialize_files_rejects_a_flat_path_materialized_after_a_nested_one(
    tmp_path: Path,
) -> None:
    # "a/b/c" materializes first, creating directory "a/b" on disk; then
    # "a\\b" (normalizing to "a/b") collides with that directory, hitting
    # os.open's IsADirectoryError rather than FileExistsError.
    content = {"a/b/c": b"nested", "a\\b": b"flat"}

    with pytest.raises(MaterializationCollisionError):
        materialize_files(content, tmp_path)


def test_materialize_files_rejects_a_nested_path_materialized_after_a_flat_one(
    tmp_path: Path,
) -> None:
    # Reverse ordering of the same collision: "a/b" materializes first as a
    # regular file, then "a\\b\\c" (normalizing to "a/b/c") needs "a/b" to be
    # a directory, hitting Path.mkdir's NotADirectoryError.
    content = {"a/b": b"flat", "a\\b\\c": b"nested"}

    with pytest.raises(MaterializationCollisionError):
        materialize_files(content, tmp_path)


# ============================================================================
# run_analyzer_subprocess — real subprocess kill-and-reap and
# bounded-read mechanisms: proving the kill-and-reap and bounded-read
# mechanisms themselves rather than only the pipeline-level isolation they
# enable.
# ============================================================================


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_timeout_kills_and_reaps_the_underlying_os_process(tmp_path: Path) -> None:
    """Proves the OS process is actually terminated once the timeout fires
    and this call returns — not merely that the Python-side await raised,
    which would leave the child running as an orphan."""
    pid_file = tmp_path / "pid.txt"

    with pytest.raises(AnalyzerTimeoutError):
        await run_analyzer_subprocess(
            ["python3", str(_TESTDATA_DIR / "fixture_write_pid_then_sleep.py"), str(pid_file)],
            cwd=tmp_path,
            timeout=1.0,
        )

    pid = int(pid_file.read_text())
    assert not _process_is_alive(pid), (
        f"pid {pid} is still alive after run_analyzer_subprocess's own timeout fired — the "
        "underlying OS process was not actually killed and reaped"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_output_cap_kills_and_reaps_a_still_running_process(tmp_path: Path) -> None:
    """A second, independent case (from the timeout case above) proving the
    bounded chunked-read byte cap: a fixture command that emits output past
    the configured cap forever (never exits on its own) is killed and
    reaped the same way, and the call fails with the typed
    ``AnalyzerOutputTooLargeError`` rather than running to completion or
    this test process's own memory growing unboundedly while reading it.
    """
    with pytest.raises(AnalyzerOutputTooLargeError):
        await run_analyzer_subprocess(
            ["python3", str(_TESTDATA_DIR / "fixture_write_forever.py")],
            cwd=tmp_path,
            timeout=10.0,
            max_output_bytes=1_000,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_double_dash_separator_reaches_the_child_process_as_a_literal_argument(
    tmp_path: Path,
) -> None:
    """``run_analyzer_subprocess`` passes ``argv`` straight through to
    ``asyncio.create_subprocess_exec`` with no shell reinterpretation —
    proving a ``--`` end-of-options separator (the mechanism
    ``adapter/analyzers/semgrep.py``'s own ``_argv`` relies on, per
    ``adapter/analyzers/test_semgrep.py``'s equivalent, concrete case) makes
    it through to the child process unmangled, using a fixture command whose
    own argparse-based CLI (the same class of parser Semgrep's own
    Click-based CLI belongs to) rejects an unrecognized `-`-prefixed
    argument unless a ``--`` precedes it.
    """
    output = await run_analyzer_subprocess(
        [
            "python3",
            str(_TESTDATA_DIR / "fixture_arg_echo.py"),
            "--",
            "-weird-name.py",
            "normal.py",
        ],
        cwd=tmp_path,
        timeout=5.0,
    )

    assert json.loads(output) == ["-weird-name.py", "normal.py"]


# ---------------------------------------------------------------------------
# L7: the product-knowledge credential map never reaches an analyzer child
# ---------------------------------------------------------------------------


def test_analyzer_env_drops_only_the_credential_map() -> None:
    env = analyzer_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/root",
            "REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS": '{"w": {"token": "t"}}',
            "REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS": "x",
            "REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST": "wiki",
        }
    )

    expected = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "REVIEW_WORKER_PRODUCT_KNOWLEDGE_ENDPOINT_ALLOWLIST": "wiki",
    }
    if env != expected:
        pytest.fail(f"env {env!r} != {expected!r}")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_child_process_does_not_inherit_the_credential_map_but_keeps_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS", "CANARY-TOKEN-xyz")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

    output = await run_analyzer_subprocess(
        [
            sys.executable,
            "-c",
            "import os;print('cred=' + repr(os.environ.get("
            "'REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS')));"
            "print('path=' + str(bool(os.environ.get('PATH'))))",
        ],
        cwd=tmp_path,
        timeout=10.0,
    )

    assert b"cred=None" in output
    assert b"CANARY" not in output
    assert b"path=True" in output
