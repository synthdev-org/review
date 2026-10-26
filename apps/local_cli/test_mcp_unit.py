"""Unit tests for the local CLI's MCP surface: the option, the settings and what they switch on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapter.local.git import ReviewRange
from adapter.local.mcp_knowledge import QUERY_MARKER_TITLE
from apps.local_cli.main import build_parser, build_seed
from apps.local_cli.settings import LocalSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def test_mcp_is_a_repeatable_option_and_is_empty_by_default() -> None:
    nominated = build_parser().parse_args(
        ["run", "--base", "main", "--mcp", "docs", "--mcp", "kb:s"]
    )
    none = build_parser().parse_args(["run", "--base", "main"])

    assert nominated.mcp == ["docs", "kb:s"] and none.mcp == []


def test_the_defaults_match_the_adr_caps() -> None:
    settings = LocalSettings()

    assert settings.mcp_config is None
    assert settings.mcp_nominations_file == ".review/mcp.json"
    assert (settings.mcp_deadline_seconds, settings.mcp_max_calls, settings.mcp_max_processes) == (
        10.0,
        3,
        3,
    )
    assert (settings.mcp_max_stdio_bytes, settings.mcp_max_list_tools_bytes) == (262_144, 65_536)
    assert settings.mcp_max_tools == 100


def test_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_MCP_CONFIG", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("REVIEW_LOCAL_MCP_DEADLINE_SECONDS", "4")
    monkeypatch.setenv("REVIEW_LOCAL_MCP_CONFIG_UNSET", "")

    settings = LocalSettings()

    assert settings.mcp_config == tmp_path / "mcp.json" and settings.mcp_deadline_seconds == 4.0


def test_a_blank_config_path_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_MCP_CONFIG", "  ")

    assert LocalSettings().mcp_config is None


def test_the_context_builder_is_on_only_when_a_server_is_nominated() -> None:
    settings = LocalSettings()

    assert settings.step_enablement().context_builder_enabled is False
    assert settings.step_enablement(mcp=True).context_builder_enabled is True


def test_the_provider_branch_has_room_for_the_scan_after_the_overall_deadline() -> None:
    settings = LocalSettings(mcp_deadline_seconds=10, mcp_scan_timeout_seconds=5)

    assert settings.pipeline_config("m").product_knowledge_deadline_seconds == 10
    assert settings.pipeline_config("m", mcp=True).product_knowledge_deadline_seconds == 21


def test_the_seed_carries_the_marker_title_only_when_a_server_is_nominated() -> None:
    review_range = ReviewRange(base_sha="b", merge_base_sha="b", head_sha="h", has_changes=True)
    plain = build_seed(Path("/repo"), review_range)
    nominated = build_seed(Path("/repo"), review_range, mcp=True)

    assert "pr_title" not in plain
    assert nominated["pr_title"] == QUERY_MARKER_TITLE
    assert {k: v for k, v in nominated.items() if k != "pr_title"} == plain
