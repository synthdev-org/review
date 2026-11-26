"""Integration test for scripts/scan-fixtures.sh against the real Gitleaks
binary: a planted secret in a scratch fixture directory fails the CI step,
and the committed eval corpus and cassettes pass it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scan-fixtures.sh"
# Same fake token adapter/analyzers/test_gitleaks.py plants.
_PLANTED_SECRET = 'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"\n'


def _scan(*dirs: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SCRIPT), *map(str, dirs)], capture_output=True, text=True, timeout=120, check=False
    )


def test_a_planted_secret_in_a_fixture_directory_fails_the_step(tmp_path: Path) -> None:
    (tmp_path / "cassette.json").write_text(_PLANTED_SECRET)

    result = _scan(tmp_path)

    assert result.returncode != 0
    # a missing binary also exits non-zero; require a real detection
    assert "leaks found" in result.stdout + result.stderr
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz12" not in result.stdout + result.stderr


def test_a_planted_secret_in_a_root_file_of_review_testdata_fails_the_step(
    tmp_path: Path,
) -> None:
    (tmp_path / "corpus.json").write_text(_PLANTED_SECRET)
    (tmp_path / "planted_secret.py").write_text(_PLANTED_SECRET)  # exempt by name
    env = {**os.environ, "SCAN_FIXTURES_ROOT_TESTDATA": str(tmp_path)}

    result = subprocess.run(
        [str(_SCRIPT)], capture_output=True, text=True, timeout=120, check=False, env=env
    )

    assert result.returncode != 0
    assert "leaks found" in result.stdout + result.stderr


def test_a_clean_directory_passes(tmp_path: Path) -> None:
    (tmp_path / "cassette.json").write_text('{"note": "nothing secret here"}\n')

    assert _scan(tmp_path).returncode == 0


def test_the_committed_fixture_directories_are_clean() -> None:
    assert _scan().returncode == 0
