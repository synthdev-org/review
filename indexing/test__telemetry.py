"""Unit test for ``indexing._telemetry.suppress_telemetry_errors`` itself.

Mirrors ``review/test__telemetry.py`` — see that module's docstring for why
a direct behavioral test is needed here too, on this package's own copy,
rather than relying solely on ``indexing/test_pipeline_telemetry.py``'s
incidental coverage or
``adapter/telemetry/test_domain_telemetry_helper_parity.py``'s
structural-only "stays in sync with its siblings" guarantee.
"""

from __future__ import annotations

import logging

import pytest

from indexing._telemetry import suppress_telemetry_errors


def test_suppress_telemetry_errors_catches_exception_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("indexing._telemetry.test")
    logger.disabled = False

    with (
        caplog.at_level("WARNING", logger=logger.name),
        suppress_telemetry_errors(logger, "recording failed", job_id="abc123"),
    ):
        raise ValueError("boom")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert record.getMessage() == "recording failed"
    assert record.job_id == "abc123"
    assert record.exc_info is not None


def test_suppress_telemetry_errors_does_not_catch_base_exception() -> None:
    logger = logging.getLogger("indexing._telemetry.test")

    with pytest.raises(KeyboardInterrupt), suppress_telemetry_errors(logger, "recording failed"):
        raise KeyboardInterrupt
