"""Unit tests for the webhook_api structured JSON logging setup."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from apps.webhook_api.logging_config import JSONFormatter, configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def test_json_formatter_produces_structured_output() -> None:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    formatted = JSONFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello world"


def test_configure_logging_emits_json_at_configured_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    logger = logging.getLogger("webhook_api.test")

    logger.debug("should be filtered out")
    logger.info("startup", extra={"environment": "development"})

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["message"] == "startup"
    assert payload["environment"] == "development"


def test_configure_logging_level_filters_lower_severity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("WARNING")
    logger = logging.getLogger("webhook_api.test2")

    logger.info("filtered")
    assert capsys.readouterr().out.strip() == ""

    logger.warning("kept")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["level"] == "WARNING"
    assert payload["message"] == "kept"
