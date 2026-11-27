"""Unit tests for findings.models — the ``Finding`` domain model, per
docs/rules/testing.md's "new domain logic a story touches still needs unit
tests too" rule. No real I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from findings.models import Finding


def _finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "file": "app.py",
        "line": 10,
        "severity": "medium",
        "message": "problem",
        "source": "llm",
    }
    defaults.update(overrides)
    return Finding.model_validate(defaults)


def test_verification_status_defaults_to_none() -> None:
    finding = _finding()

    assert finding.verification_status is None


def test_verification_status_accepts_verified() -> None:
    finding = _finding(verification_status="verified")

    assert finding.verification_status == "verified"


def test_verification_status_accepts_rejected() -> None:
    finding = _finding(verification_status="rejected")

    assert finding.verification_status == "rejected"


def test_verification_status_accepts_explicit_none() -> None:
    finding = _finding(verification_status=None)

    assert finding.verification_status is None


def test_verification_status_rejects_an_invalid_value() -> None:
    with pytest.raises(ValidationError):
        _finding(verification_status="maybe")


def test_category_defaults_to_none_and_round_trips() -> None:
    assert _finding().category is None
    finding = _finding(category="smell")

    assert finding.category == "smell"
    assert Finding.model_validate(finding.model_dump()) == finding
    assert hash(finding) == hash(_finding(category="smell"))


def test_category_rejects_an_invalid_value() -> None:
    with pytest.raises(ValidationError):
        _finding(category="style")
