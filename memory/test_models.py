"""Unit tests for memory.models — the ``MemoryEntry``/``PostedComment``
domain models, per docs/rules/testing.md's "new domain logic a story
touches still needs unit tests" rule. No real I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memory.models import MemoryEntry, PostedComment

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _entry(**overrides: object) -> MemoryEntry:
    defaults: dict[str, object] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "repo_id": "22222222-2222-2222-2222-222222222222",
        "org_id": None,
        "team_id": None,
        "entry_type": None,
        "status": "pending",
        "raw_signal_type": "reply",
        "raw_signal_detail": {"body": "not applicable here"},
        "posted_finding_comment_id": 12345,
        "embedding": None,
        "approved_at": None,
        "approved_by": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return MemoryEntry.model_validate(defaults)


def test_a_freshly_captured_entry_has_no_entry_type_or_approval() -> None:
    entry = _entry()

    assert entry.entry_type is None
    assert entry.status == "pending"
    assert entry.approved_at is None
    assert entry.approved_by is None
    assert entry.embedding is None


def test_an_approved_dismissed_finding_entry_carries_its_own_embedding() -> None:
    entry = _entry(
        status="approved",
        entry_type="dismissed_finding",
        embedding=(0.1, 0.2, 0.3),
        approved_at=_NOW,
        approved_by="static-admin-key",
    )

    assert entry.status == "approved"
    assert entry.entry_type == "dismissed_finding"
    assert entry.embedding == (0.1, 0.2, 0.3)
    assert entry.approved_by == "static-admin-key"


def test_an_invalid_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _entry(status="archived")


def test_an_invalid_entry_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _entry(entry_type="not_a_real_type")


def test_an_invalid_raw_signal_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _entry(raw_signal_type="edit")


def test_memory_entry_is_frozen() -> None:
    entry = _entry()

    with pytest.raises(ValidationError):
        entry.status = "approved"  # type: ignore[misc]


def test_posted_comment_carries_the_finding_it_correlates_to() -> None:
    comment = PostedComment(
        comment_id=987654321, file="app/main.py", line=42, rule_id="no-secrets", message="found one"
    )

    assert comment.comment_id == 987654321
    assert comment.file == "app/main.py"
    assert comment.line == 42
    assert comment.rule_id == "no-secrets"
    assert comment.message == "found one"


def test_posted_comment_allows_a_file_level_finding_with_no_line() -> None:
    comment = PostedComment(comment_id=1, file="app/main.py", line=None, rule_id=None, message="x")

    assert comment.line is None
    assert comment.rule_id is None


def test_posted_comment_is_frozen() -> None:
    comment = PostedComment(comment_id=1, file="app/main.py", line=None, rule_id=None, message="x")

    with pytest.raises(ValidationError):
        comment.message = "changed"  # type: ignore[misc]
