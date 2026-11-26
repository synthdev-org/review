"""Unit tests for tenancy.settings's pure logic, per docs/rules/testing.md's
"new domain logic a story touches still needs unit tests" rule: unit tests
for the pure precedence-resolution function across platform/org/repo/team
combinations. No I/O, no fakes needed — every function under test here is
pure.
"""

from __future__ import annotations

import pytest

from tenancy.settings import (
    ScopeDescriptorError,
    SettingRow,
    SettingValidationError,
    effective_settings,
    format_scope_descriptor,
    parse_scope_descriptor,
    resolve_setting,
    validate_endpoint_url,
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


# --- product_knowledge_provider --

_PK_KEY = "product_knowledge_provider"


def _pk_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "display_name": "Company Wiki (prod)",
        "endpoint_url": "https://wiki.internal/mcp",
        "credential_ref": "wiki-prod",
    }
    value.update(overrides)
    return value


def _pk_row(
    *, org_id: str | None, repo_id: str | None, team_id: str | None = None, **overrides: object
) -> SettingRow:
    return SettingRow(
        org_id=org_id, repo_id=repo_id, team_id=team_id, key=_PK_KEY, value=_pk_value(**overrides)
    )


class TestProductKnowledgeProviderRegistryModel:
    def test_valid_value_round_trips_with_defaults_filled_in(self) -> None:
        stored = validate_setting_value(_PK_KEY, _pk_value())

        assert stored == {
            "enabled": True,
            "display_name": "Company Wiki (prod)",
            "endpoint_url": "https://wiki.internal/mcp",
            "credential_ref": "wiki-prod",
            "allow_public_repos": False,
            "issue_key_prefixes": [],
        }

    def test_null_credential_ref_is_valid_for_an_unauthenticated_provider(self) -> None:
        stored = validate_setting_value(_PK_KEY, _pk_value(credential_ref=None))

        assert stored["credential_ref"] is None

    def test_endpoint_url_is_stored_unchanged(self) -> None:
        stored = validate_setting_value(_PK_KEY, _pk_value(endpoint_url="http://kb:8080"))

        assert stored["endpoint_url"] == "http://kb:8080"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"display_name": ""},
            {"display_name": "x" * 41},
            {"display_name": "<script>"},
            {"display_name": "name\nnewline"},
            {"endpoint_url": "ftp://wiki.internal/mcp"},
            {"endpoint_url": "/relative/path"},
            {"endpoint_url": "https://user:pw@wiki.internal/mcp"},
            {"endpoint_url": "https://wiki.internal/mcp?token=abc"},
            {"endpoint_url": "https://wiki.internal/mcp#frag"},
            {"endpoint_url": "https://wiki.internal:notaport/mcp"},
            {"endpoint_url": "https://" + "a" * 520 + ".example"},
            {"endpoint_url": "https://wiki.internal/ mcp"},
            {"credential_ref": "Has Caps"},
            {"credential_ref": "x" * 65},
            {"credential_ref": ""},
            {"allow_public_repos": "yes please"},
            {"issue_key_prefixes": ["lower"]},
            {"issue_key_prefixes": ["A"]},
            {"issue_key_prefixes": ["TOOLONGPREFIX"]},
            {"issue_key_prefixes": ["OK"] * 21},
            {"unknown_field": 1},
        ],
    )
    def test_invalid_values_are_rejected_not_coerced(self, overrides: dict[str, object]) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value(_PK_KEY, _pk_value(**overrides))

    def test_endpoint_validation_error_never_echoes_userinfo(self) -> None:
        with pytest.raises(SettingValidationError) as excinfo:
            validate_setting_value(
                _PK_KEY, _pk_value(endpoint_url="https://admin:hunter2pw@wiki.internal/mcp")
            )

        assert "userinfo" in excinfo.value.detail
        assert "hunter2pw" not in excinfo.value.detail
        assert "hunter2pw" not in str(excinfo.value)


class TestEffectiveSettings:
    def test_repo_row_wins_over_org_and_platform_whole_object(self) -> None:
        rows = [
            _pk_row(org_id=None, repo_id=None, display_name="Platform"),
            _pk_row(org_id=_ORG, repo_id=None, display_name="Org", credential_ref=None),
            _pk_row(org_id=_ORG, repo_id=_REPO, display_name="Repo", endpoint_url="http://r/mcp"),
        ]

        effective = effective_settings(rows, org_id=_ORG, repo_id=_REPO, team_id=None)

        assert effective[_PK_KEY].source == f"repo:{_ORG}:{_REPO}"
        # Whole object, no field merging: the repo row's own credential_ref.
        assert effective[_PK_KEY].value["display_name"] == "Repo"
        assert effective[_PK_KEY].value["credential_ref"] == "wiki-prod"

    def test_org_row_is_inherited_by_a_repo_without_its_own(self) -> None:
        rows = [_pk_row(org_id=_ORG, repo_id=None, display_name="Org")]

        effective = effective_settings(rows, org_id=_ORG, repo_id=_REPO, team_id=None)

        assert effective[_PK_KEY].source == f"org:{_ORG}"

    def test_repo_enabled_false_is_the_effective_value_an_explicit_opt_out(self) -> None:
        rows = [
            _pk_row(org_id=None, repo_id=None),
            _pk_row(org_id=_ORG, repo_id=_REPO, enabled=False),
        ]

        effective = effective_settings(rows, org_id=_ORG, repo_id=_REPO, team_id=None)

        assert effective[_PK_KEY].value["enabled"] is False
        assert effective[_PK_KEY].source == f"repo:{_ORG}:{_REPO}"

    def test_platform_enabled_false_is_the_fleet_wide_no_restart_off(self) -> None:
        rows = [_pk_row(org_id=None, repo_id=None, enabled=False)]

        effective = effective_settings(rows, org_id=_ORG, repo_id=_REPO, team_id=None)

        assert effective[_PK_KEY].value["enabled"] is False
        assert effective[_PK_KEY].source == "platform"

    def test_team_scoped_provider_row_is_ignored(self) -> None:
        rows = [
            _pk_row(org_id=_ORG, repo_id=None, display_name="Org"),
            _pk_row(org_id=_ORG, repo_id=None, team_id=_TEAM, display_name="Team"),
        ]

        effective = effective_settings(rows, org_id=_ORG, repo_id=None, team_id=_TEAM)

        assert effective[_PK_KEY].value["display_name"] == "Org"

    def test_an_invalid_stored_row_is_unconfigured_not_effective_and_does_not_fall_through(
        self,
    ) -> None:
        rows = [
            _pk_row(org_id=None, repo_id=None, display_name="Platform"),
            _pk_row(org_id=_ORG, repo_id=None, endpoint_url="ftp://x"),
        ]

        effective = effective_settings(rows, org_id=_ORG, repo_id=_REPO, team_id=None)

        assert _PK_KEY not in effective

    def test_orphaned_keys_outside_the_registry_are_not_resolved(self) -> None:
        rows = [
            SettingRow(org_id=None, repo_id=None, team_id=None, key="retired_key", value={"x": 1})
        ]

        assert effective_settings(rows, org_id=None, repo_id=None, team_id=None) == {}


class TestValidateEndpointUrl:
    @pytest.mark.parametrize(
        "value",
        [
            "https://wiki.internal/mcp",
            "http://kb:8080",
            "https://[::1]:8443/mcp",
            "https://wiki.internal/" + "a" * 480,
        ],
    )
    def test_accepts_and_returns_unchanged(self, value: str) -> None:
        assert validate_endpoint_url(value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("https://wiki.internal/" + "a" * 600, "printable ASCII"),
            ("https://wiki.internal/\u00e9", "printable ASCII"),
            ("https://wiki.internal/a b", "printable ASCII"),
            ("https://wiki.internal/\x00", "printable ASCII"),
            ("https://wiki.internal:99999/x", "not a valid URL"),
            ("ftp://wiki.internal/x", "absolute http.s. URL with a host"),
            ("/relative/path", "absolute http.s. URL with a host"),
            ("https:///x", "absolute http.s. URL with a host"),
            ("https://user:pw@wiki.internal/x", "userinfo"),
            ("https://user@wiki.internal/x", "userinfo"),
            ("https://wiki.internal/x?t=1", "query or fragment"),
            ("https://wiki.internal/x#f", "query or fragment"),
            ("https://wiki.internal/x?", "query or fragment"),
            ("https://wiki.internal/x#", "query or fragment"),
        ],
    )
    def test_rejects_with_fixed_message(self, value: str, message: str) -> None:
        with pytest.raises(ValueError, match=message) as info:
            validate_endpoint_url(value)
        assert value not in str(info.value)


class TestFindingsShowNitsSetting:
    def test_valid_value_round_trips(self) -> None:
        assert validate_setting_value("findings_show_nits", {"enabled": True}) == {"enabled": True}

    def test_non_bool_and_missing_values_are_rejected(self) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value("findings_show_nits", {"enabled": "maybe"})
        with pytest.raises(SettingValidationError):
            validate_setting_value("findings_show_nits", {})


def _classifier_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": False,
        "backend": "typesafe",
        "endpoint_url": "https://api.typesafe.ai",
        "credential_ref": "typesafe",
        "model": "jev-latest",
        "mode": "shadow",
    }
    return value | overrides


class TestClassifierProviderSetting:
    def test_valid_value_round_trips_with_acknowledgement_defaulting_false(self) -> None:
        stored = validate_setting_value("classifier_provider", _classifier_value())

        assert stored == _classifier_value() | {"disclosure_acknowledged": False}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"backend": "openai"},
            {"mode": "off"},
            {"enabled": "maybe"},
            {"endpoint_url": "http://api.typesafe.ai"},
            {"endpoint_url": "https://user:pw@api.typesafe.ai"},
            {"endpoint_url": "https://api.typesafe.ai/?k=v"},
            {"endpoint_url": "https://api.typesafe.ai/#f"},
            {"endpoint_url": "api.typesafe.ai"},
            {"credential_ref": "Has Caps"},
            {"credential_ref": ""},
            {"credential_ref": None},
            {"model": ""},
            {"model": "-leading-dash"},
            {"model": "has space"},
            {"model": "x" * 65},
            {"unknown_field": 1},
        ],
    )
    def test_invalid_shapes_are_rejected(self, overrides: dict[str, object]) -> None:
        with pytest.raises(SettingValidationError):
            validate_setting_value("classifier_provider", _classifier_value(**overrides))

    def test_team_rows_are_ignored_when_resolving_the_effective_value(self) -> None:
        row = SettingRow(
            org_id=_ORG,
            repo_id=None,
            team_id=_TEAM,
            key="classifier_provider",
            value=_classifier_value(),
        )

        assert "classifier_provider" not in effective_settings(
            [row], org_id=_ORG, repo_id=None, team_id=_TEAM
        )
