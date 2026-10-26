"""Unit tests for apps.local_cli.main: argument parsing, logging and configuration errors."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from apps.local_cli.main import EXIT_USAGE, build_parser, configure_logging, main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def test_run_requires_a_base_or_staged() -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["run"])
    assert caught.value.code == 2


def test_parses_run_with_base_and_verbosity() -> None:
    args = build_parser().parse_args(["-vv", "run", "--base", "main"])

    assert (args.command, args.base, args.staged, args.verbose) == ("run", "main", False, 2)


@pytest.mark.parametrize(
    ("verbosity", "level"), [(0, logging.WARNING), (1, logging.INFO), (5, logging.DEBUG)]
)
def test_verbosity_selects_the_log_level(verbosity: int, level: int) -> None:
    configure_logging(verbosity)

    assert logging.getLogger("review").level == level
    assert logging.getLogger().level == logging.WARNING


def test_logs_go_to_stderr_and_show_only_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(1)

    logging.getLogger("review.demo").info("step finished", extra={"diff": "SECRET-CODE"})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "step finished" in captured.err
    assert "SECRET-CODE" not in captured.err


def test_invalid_configuration_exits_2_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_LOCAL_LLM_ENDPOINT", "sk-looks-like-a-key")

    assert main(["run", "--base", "main"]) == EXIT_USAGE

    err = capsys.readouterr().err
    assert "invalid configuration" in err
    assert "llm_endpoint" in err
    assert "sk-looks-like-a-key" not in err


def test_logs_show_the_step_name_but_no_other_extras(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(1)

    logging.getLogger("review.pipeline").info(
        "step completed", extra={"step_name": "prefilter", "duration_ms": 12, "error": "SECRET"}
    )

    err = capsys.readouterr().err
    assert "step completed [step_name=prefilter duration_ms=12]" in err
    assert "SECRET" not in err


def test_vv_leaks_no_third_party_debug_or_exception_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(2)
    token = "PLANTED-TOKEN-xyz"

    logging.getLogger("openai._base_client").debug("Request options: %s", {"prompt": token})
    logging.getLogger("httpx").info("HTTP Request body %s", token)
    logging.getLogger("LiteLLM").debug("headers Authorization: Bearer %s", token)
    try:
        raise RuntimeError(f"response body {token}")
    except RuntimeError:
        logging.getLogger("review._telemetry").warning("telemetry failed", exc_info=True)

    err = capsys.readouterr().err
    assert "telemetry failed" in err
    assert token not in err
    assert "Traceback" not in err


def test_the_command_takes_exactly_one_of_base_and_staged() -> None:
    parser = build_parser()

    assert parser.parse_args(["run", "--staged"]).staged is True
    assert parser.parse_args(["run", "--base", "main", "--output", "r.md"]).output == Path("r.md")
    for argv in (["run"], ["run", "--base", "main", "--staged"]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2


def _subcommand_help(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["run", "--help"])
    assert caught.value.code == 0
    return capsys.readouterr().out


def test_run_help_lists_every_environment_variable(capsys: pytest.CaptureFixture[str]) -> None:
    from apps.local_cli.settings import LocalSettings

    text = _subcommand_help(capsys)
    for name, field in LocalSettings.model_fields.items():
        alias = field.validation_alias
        env = alias if isinstance(alias, str) else f"REVIEW_LOCAL_{name.upper()}"
        assert env in text, f"{env} is not documented in 'review run --help'"


def test_run_help_lists_every_option_and_the_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _subcommand_help(capsys)

    for option in (
        "--base",
        "--staged",
        "--dry-run",
        "--output",
        "--mcp",
        "--allow-claude-cli-in-ci",
    ):
        assert option in text
    assert "exit codes:" in text and "3  severity gate tripped" in text


def test_version_flag_prints_the_version_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out.startswith("review ")


def test_a_missing_ref_tells_the_user_what_to_pass() -> None:
    from adapter.local.exceptions import GitRefNotFoundError

    assert "--base main" in str(GitRefNotFoundError(ref="nope", shallow=False))
