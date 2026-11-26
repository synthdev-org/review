from __future__ import annotations

import pytest
from pydantic import ValidationError

from ingestion.commits import CommitInfo, CommitsUnavailableError


def test_commit_info_defaults_and_is_frozen() -> None:
    c = CommitInfo(sha="a", parent_shas=("b",))
    assert c.title == ""
    with pytest.raises(ValidationError):
        c.sha = "x"  # type: ignore[misc]


def test_commits_unavailable_error_carries_fields() -> None:
    err = CommitsUnavailableError(repo_full_name="o/r", detail="boom")
    assert err.repo_full_name == "o/r"
    assert err.detail == "boom"
    assert "o/r" in str(err) and "boom" in str(err)
