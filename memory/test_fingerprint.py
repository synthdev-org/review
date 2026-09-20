"""Unit tests for memory.fingerprint.build_finding_fingerprint — pure,
no-I/O text composition, per ADR-0005's "pure domain logic is Unit-tested
directly" precedent.
"""

from __future__ import annotations

from memory.fingerprint import build_finding_fingerprint


def test_fingerprint_joins_file_rule_id_and_message() -> None:
    fingerprint = build_finding_fingerprint(
        file="app/foo.py", rule_id="B101", message="hardcoded password"
    )
    assert fingerprint == "app/foo.py\nB101\nhardcoded password"


def test_fingerprint_uses_empty_string_for_missing_rule_id() -> None:
    fingerprint = build_finding_fingerprint(file="app/foo.py", rule_id=None, message="looks off")
    assert fingerprint == "app/foo.py\n\nlooks off"


def test_fingerprint_is_deterministic_for_the_same_inputs() -> None:
    first = build_finding_fingerprint(file="a.py", rule_id="R1", message="m")
    second = build_finding_fingerprint(file="a.py", rule_id="R1", message="m")
    assert first == second


def test_fingerprint_differs_when_any_field_differs() -> None:
    base = build_finding_fingerprint(file="a.py", rule_id="R1", message="m")
    assert base != build_finding_fingerprint(file="b.py", rule_id="R1", message="m")
    assert base != build_finding_fingerprint(file="a.py", rule_id="R2", message="m")
    assert base != build_finding_fingerprint(file="a.py", rule_id="R1", message="n")
