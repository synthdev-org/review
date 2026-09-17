"""Unit test for ``review._telemetry.suppress_telemetry_errors`` itself.

docs/rules/testing.md requires unit tests co-located as ``foo.py`` /
``test_foo.py`` in the same package — every real call site
(``review/pipeline.py``, ``review/verification.py``, ``review/output.py``,
``review/context_builder.py``, ``review/prefilter.py``,
``review/review_generation.py``) exercises this context manager only
incidentally, and
``adapter/telemetry/test_domain_telemetry_helper_parity.py`` only proves
this module's copy stays byte-identical to its siblings in
``indexing._telemetry``/``adapter.telemetry.sdk`` — neither actually proves
the context manager's own behavior (catch-log-continue for an ordinary
``Exception``, propagate a ``BaseException`` unchanged). These two tests
close that gap directly against this package's own copy.
"""

from __future__ import annotations

import logging

import pytest

from review._telemetry import suppress_telemetry_errors


def test_suppress_telemetry_errors_catches_exception_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("review._telemetry.test")
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
    logger = logging.getLogger("review._telemetry.test")

    with pytest.raises(KeyboardInterrupt), suppress_telemetry_errors(logger, "recording failed"):
        raise KeyboardInterrupt
