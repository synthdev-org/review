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


def test_v2_prompt_rewording_changes_the_fingerprint_but_scenario_and_tier_do_not() -> None:
    """The fingerprint is ``file/rule_id/message`` only. The v2 prompt rewrites the
    wording of ``message``, so a finding dismissed under v1 wording embeds
    differently under v2 wording and can re-surface once on the first v2 run
    (accepted, documented in release notes). ``scenario``/``tier`` are not
    inputs, so adding them cannot by itself change a fingerprint."""
    v1 = build_finding_fingerprint(file="a.py", rule_id=None, message="Possible null deref")
    v2 = build_finding_fingerprint(
        file="a.py", rule_id=None, message="a.py:3 dereferences a value that is None on retry"
    )
    assert v1 != v2
    assert v1 == build_finding_fingerprint(file="a.py", rule_id=None, message="Possible null deref")
