"""Unit tests for tenancy.settings's pure logic, per docs/rules/testing.md's
"new domain logic a story touches still needs unit tests" rule and
docs/plans/plan-deliver-admin-gui.md's Phase 4 Tests section ("unit tests
for the pure precedence-resolution function across platform/org/repo/team
combinations"). No I/O, no fakes needed — every function under test here is
pure.
"""

from __future__ import annotations

import pytest

from tenancy.settings import (
    ScopeDescriptorError,
    SettingRow,
    SettingValidationError,
    format_scope_descriptor,
    parse_scope_descriptor,
    resolve_setting,
    validate_setting_value,
)

_ORG = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG = "22222222-2222-2222-2222-222222222222"
_REPO = "33333333-3333-3333-3333-333333333333"
_TEAM = "44444444-4444-4444-4444-444444444444"


def _row(
    *, org_id: str | None, repo_id: str | None, team_id: str | None, value: object
) -> SettingRow:
    return SettingRow(
        org_id=org_id,
        repo_id=repo_id,
        team_id=team_id,
        key="memory_suppression_enabled",
        value=value,
    )


class TestResolveSettingPrecedence:
    """Platform < org < repo/team, per US-A13's second Given/When/Then."""

    def test_platform_only_resolves_for_any_scope(self) -> None:
        platform_row = _row(org_id=None, repo_id=None, team_id=None, value={"enabled": True})
        rows = [platform_row]

        assert resolve_setting("memory_suppression_enabled", rows) is platform_row
        assert resolve_setting("memory_suppression_enabled", rows, org_id=_ORG) is platform_row
        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, repo_id=_REPO)
            is platform_row
        )
        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, team_id=_TEAM)
            is platform_row
        )

    def test_org_row_overrides_platform_for_that_org_and_its_repos_teams(self) -> None:
        platform_row = _row(org_id=None, repo_id=None, team_id=None, value={"enabled": True})
        org_row = _row(org_id=_ORG, repo_id=None, team_id=None, value={"enabled": False})
        rows = [platform_row, org_row]

        assert resolve_setting("memory_suppression_enabled", rows, org_id=_ORG) is org_row
        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, repo_id=_REPO)
            is org_row
        )
        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, team_id=_TEAM)
            is org_row
        )
        # A different org with no row of its own still falls all the way
        # back to platform, never the unrelated org's row.
        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_OTHER_ORG) is platform_row
        )

    def test_repo_row_overrides_org_row_for_that_repo_only(self) -> None:
        org_row = _row(org_id=_ORG, repo_id=None, team_id=None, value={"enabled": False})
        repo_row = _row(org_id=_ORG, repo_id=_REPO, team_id=None, value={"enabled": True})
        rows = [org_row, repo_row]

        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, repo_id=_REPO)
            is repo_row
        )
        # The org itself (no repo_id given) still resolves to the org row,
        # not the repo-scoped override.
        assert resolve_setting("memory_suppression_enabled", rows, org_id=_ORG) is org_row

    def test_team_row_overrides_org_row_for_that_team_only(self) -> None:
        org_row = _row(org_id=_ORG, repo_id=None, team_id=None, value={"enabled": False})
        team_row = _row(org_id=_ORG, repo_id=None, team_id=_TEAM, value={"enabled": True})
        rows = [org_row, team_row]

        assert (
            resolve_setting("memory_suppression_enabled", rows, org_id=_ORG, team_id=_TEAM)
            is team_row
        )
        assert resolve_setting("memory_suppression_enabled", rows, org_id=_ORG) is org_row

    def test_no_matching_row_at_any_scope_returns_none(self) -> None:
        assert resolve_setting("memory_suppression_enabled", [], org_id=_ORG, repo_id=_REPO) is None

    def test_only_rows_matching_the_requested_key_are_considered(self) -> None:
        other_key_row = SettingRow(
            org_id=None, repo_id=None, team_id=None, key="findings_severity_threshold", value={}
        )
        assert resolve_setting("memory_suppression_enabled", [other_key_row]) is None


class TestValidateSettingValue:
    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(SettingValidationError) as excinfo:
            validate_setting_value("not_a_real_key", {"anything": True})
        assert excinfo.value.key == "not_a_real_key"

    def test_known_key_with_valid_value_round_trips(self) -> None:
        validated = validate_setting_value("memory_suppression_enabled", {"enabled": True})
        assert validated == {"enabled": True}

    def test_known_key_with_malformed_value_is_rejected_not_coerced(self) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value("memory_suppression_enabled", {"enabled": "not-a-bool"})

    def test_known_key_with_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value("memory_suppression_enabled", {})

    def test_enum_key_rejects_a_value_outside_the_closed_set(self) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value("findings_severity_threshold", {"threshold": "apocalyptic"})

    def test_enum_key_accepts_a_known_value(self) -> None:
        validated = validate_setting_value("findings_severity_threshold", {"threshold": "high"})
        assert validated == {"threshold": "high"}


class TestScopeDescriptor:
    def test_platform_round_trips(self) -> None:
        assert parse_scope_descriptor("platform") == (None, None, None)
        assert format_scope_descriptor(org_id=None, repo_id=None, team_id=None) == "platform"

    def test_org_round_trips(self) -> None:
        assert parse_scope_descriptor(f"org:{_ORG}") == (_ORG, None, None)
        assert format_scope_descriptor(org_id=_ORG, repo_id=None, team_id=None) == f"org:{_ORG}"

    def test_repo_round_trips(self) -> None:
        assert parse_scope_descriptor(f"repo:{_ORG}:{_REPO}") == (_ORG, _REPO, None)
        assert (
            format_scope_descriptor(org_id=_ORG, repo_id=_REPO, team_id=None)
            == f"repo:{_ORG}:{_REPO}"
        )

    def test_team_round_trips(self) -> None:
        assert parse_scope_descriptor(f"team:{_ORG}:{_TEAM}") == (_ORG, None, _TEAM)
        assert (
            format_scope_descriptor(org_id=_ORG, repo_id=None, team_id=_TEAM)
            == f"team:{_ORG}:{_TEAM}"
        )

    @pytest.mark.parametrize(
        "descriptor",
        [
            "",
            "bogus",
            "org:",
            "org",
            "repo:only-one-id",
            f"repo::{_REPO}",
            f"team:{_ORG}:",
            "platform:extra",
        ],
    )
    def test_malformed_descriptor_is_rejected(self, descriptor: str) -> None:
        with pytest.raises(ScopeDescriptorError):
            parse_scope_descriptor(descriptor)
