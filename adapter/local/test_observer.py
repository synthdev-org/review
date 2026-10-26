"""Unit tests for the run observer: progress lines and outcomes from the pipeline's log records,
and that nothing but names, statuses and durations is ever read from them."""

from __future__ import annotations

import io
import logging

import pytest

from adapter.local.observer import RunObserver, observing
from adapter.local.summary import AnalyzerOutcome

_ANALYZERS = ("ruff", "semgrep")


def _observer(
    preset: dict[str, AnalyzerOutcome] | None = None,
) -> tuple[RunObserver, io.StringIO]:
    progress = io.StringIO()
    return RunObserver(analyzer_names=_ANALYZERS, preset=preset or {}, progress=progress), progress


def _emit(observer: RunObserver, logger: str, message: str, **extra: object) -> None:
    record = logging.getLogger(logger).makeRecord(
        logger, logging.INFO, __file__, 0, message, (), None, extra=extra
    )
    observer.handle(record)


def test_stages_are_reported_started_and_finished_with_their_duration() -> None:
    observer, progress = _observer()

    _emit(observer, "review.pipeline", "step started", step_name="prefilter")
    _emit(
        observer,
        "review.pipeline",
        "step completed",
        step_name="prefilter",
        status="success",
        duration_ms=12,
    )

    assert progress.getvalue() == "  prefilter ...\n  prefilter: success (12 ms)\n"
    assert observer.stages["prefilter"].duration_ms == 12


def test_a_disabled_stage_is_recorded_without_a_progress_line() -> None:
    observer, progress = _observer()

    _emit(
        observer,
        "review.pipeline",
        "step skipped: disabled",
        step_name="walkthrough",
        status="disabled",
    )

    assert progress.getvalue() == ""
    assert observer.stages["walkthrough"].status == "disabled"


def test_analyzer_outcomes_used_errored_and_timed_out() -> None:
    observer, _ = _observer()

    _emit(observer, "review.pipeline", "step completed", step_name="ruff", status="success")
    _emit(
        observer, "review.pipeline", "step isolated: failed", step_name="semgrep", status="timeout"
    )

    assert observer.analyzers["ruff"].status == "used"
    assert (observer.analyzers["semgrep"].status, observer.analyzers["semgrep"].reason) == (
        "errored",
        "timed out",
    )
    assert observer.stages == {}  # analyzers are not stages


def test_an_analyzer_with_no_matching_files_stays_skipped_when_it_then_completes() -> None:
    observer, _ = _observer()

    _emit(
        observer,
        "review.static_analysis",
        "analyzer skipped: no matching files",
        analyzer_name="ruff",
    )
    _emit(observer, "review.pipeline", "step completed", step_name="ruff", status="success")

    assert observer.analyzers["ruff"].reason == "no matching files"
    assert observer.analyzers["ruff"].status == "skipped"


def test_a_preset_skip_is_never_overridden() -> None:
    preset = {"semgrep": AnalyzerOutcome(name="semgrep", status="skipped", reason="not installed")}
    observer, _ = _observer(preset)

    _emit(
        observer,
        "review.pipeline",
        "step skipped: disabled",
        step_name="semgrep",
        status="disabled",
    )

    assert observer.analyzers["semgrep"].reason == "not installed"


def test_error_text_and_other_extras_never_reach_progress_or_outcomes() -> None:
    observer, progress = _observer()

    _emit(
        observer,
        "review.pipeline",
        "step failed",
        step_name="review_generation",
        status="failed",
        duration_ms=5,
        error="SECRET-CODE from the diff",
    )

    assert "SECRET" not in progress.getvalue()
    assert "SECRET" not in observer.stages["review_generation"].model_dump_json()


def test_observing_hears_info_records_and_restores_the_loggers_afterwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer, progress = _observer()
    pipeline = logging.getLogger("review.pipeline")
    monkeypatch.setattr(pipeline, "level", logging.WARNING)  # restored by monkeypatch afterwards

    with observing(observer):
        pipeline.info("step started", extra={"step_name": "prefilter"})
    pipeline.info("step started", extra={"step_name": "after"})

    assert "prefilter" in progress.getvalue()
    assert "after" not in progress.getvalue()
    assert pipeline.level == logging.WARNING
    assert observer not in pipeline.handlers


def test_observing_hears_a_logger_an_earlier_fileconfig_disabled_and_restores_it() -> None:
    logger = logging.getLogger("review.review_generation")
    logger.disabled = True  # what Alembic's fileConfig() does to every existing logger
    observer, _ = _observer()
    try:
        with observing(observer):
            logger.warning(
                "review_generation: diff truncated to fit prompt byte ceiling",
                extra={"original_bytes": 9, "truncated_bytes": 4, "ceiling_bytes": 4},
            )
        assert observer.truncation.status == "truncated"
        assert logger.disabled is True
    finally:
        logger.disabled = False
