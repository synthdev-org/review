"""adapter.local.observer — per-stage progress and outcomes, from the pipeline's own log lines.

``StepRegistry`` reports every step start, completion, failure and skip as a log record carrying
``step_name``/``status``/``duration_ms`` (and the static-analysis step reports analyzers the same
way). Listening to those records gives progress and the run summary without touching the shared
pipeline. Only names, statuses and durations are read from them; ``error`` text and everything
else a record carries is ignored, so no code content can reach stderr or the summary.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TextIO

from adapter.local.summary import AnalyzerOutcome, StageOutcome, TruncationSummary

# The loggers that carry those records: the registry itself, and the static-analysis sub-step that
# reports an analyzer with no matching files.
_PIPELINE_LOGGERS = (
    "review.pipeline",
    "review.static_analysis",
    # The LLM steps warn when the size ceiling cuts the diff; that is the truncation report.
    "review.intent_summary",
    "review.review_generation",
    "review.verification",
)
_TRUNCATED_SUFFIX = "diff truncated to fit prompt byte ceiling"
_STAGE_STATUSES = {"success", "failed", "timeout", "disabled"}
_ERRORED_REASON = {"failed": "failed", "timeout": "timed out"}


class RunObserver(logging.Handler):
    """Collects stage and analyzer outcomes and prints one progress line per stage event.

    ``analyzer_names`` tells analyzer sub-steps (reported by the inner registry under the analyzer's
    own name) from macro stages. ``preset`` are analyzers review itself decided not to run; a later
    record never overrides them.
    """

    def __init__(
        self,
        *,
        analyzer_names: tuple[str, ...],
        preset: Mapping[str, AnalyzerOutcome],
        progress: TextIO,
    ) -> None:
        super().__init__(level=logging.INFO)
        self._analyzer_names = set(analyzer_names)
        self._progress = progress
        self.stages: dict[str, StageOutcome] = {}
        self.analyzers: dict[str, AnalyzerOutcome] = dict(preset)
        self._preset = set(preset)
        self.truncation = TruncationSummary()

    def emit(self, record: logging.LogRecord) -> None:
        fields = record.__dict__
        message = record.getMessage()
        if message.endswith(_TRUNCATED_SUFFIX):
            self._observe_truncation(fields)
            return
        if message == "analyzer skipped: no matching files":
            analyzer = str(fields.get("analyzer_name"))
            if analyzer not in self._preset:
                self.analyzers[analyzer] = AnalyzerOutcome(
                    name=analyzer, status="skipped", reason="no matching files"
                )
            return
        name = fields.get("step_name")
        if not isinstance(name, str):
            return
        status = fields.get("status")
        if name in self._analyzer_names:
            self._observe_analyzer(name, message, status)
        else:
            self._observe_stage(name, message, status, fields.get("duration_ms"))

    def _observe_truncation(self, fields: Mapping[str, object]) -> None:
        original, kept, ceiling = (
            fields.get("original_bytes"),
            fields.get("truncated_bytes"),
            fields.get("ceiling_bytes"),
        )
        if not all(isinstance(v, int) for v in (original, kept, ceiling)):
            return
        self.truncation = TruncationSummary(
            status="truncated",
            detail=f"diff cut from {original} to {kept} bytes by the {ceiling}-byte ceiling",
        )

    def _observe_analyzer(self, name: str, message: str, status: object) -> None:
        if name in self._preset:
            return
        current = self.analyzers.get(name)
        if message == "step completed":
            # A "no matching files" skip returns normally, so it also completes: keep the skip.
            if current is None:
                self.analyzers[name] = AnalyzerOutcome(name=name, status="used")
        elif status in _ERRORED_REASON:
            self.analyzers[name] = AnalyzerOutcome(
                name=name, status="errored", reason=_ERRORED_REASON[str(status)]
            )
        elif status == "disabled":
            self.analyzers[name] = AnalyzerOutcome(name=name, status="skipped", reason="disabled")

    def _observe_stage(self, name: str, message: str, status: object, duration: object) -> None:
        if message == "step started":
            print(f"  {name} ...", file=self._progress, flush=True)
            return
        if status not in _STAGE_STATUSES:
            return
        duration_ms = duration if isinstance(duration, int) else None
        outcome = StageOutcome.model_validate(
            {"name": name, "status": status, "duration_ms": duration_ms}
        )
        self.stages[name] = outcome
        if status != "disabled":
            took = "" if duration_ms is None else f" ({duration_ms} ms)"
            print(f"  {name}: {status}{took}", file=self._progress, flush=True)


@contextmanager
def observing(observer: RunObserver) -> Iterator[None]:
    """Attach ``observer`` to the pipeline loggers for the duration of a run. Their level is
    lowered to INFO so the records exist; the stderr log handler keeps its own level, so ``-v``
    behaves as before."""
    loggers = [logging.getLogger(name) for name in _PIPELINE_LOGGERS]
    previous = [(logger.level, logger.disabled) for logger in loggers]
    for logger in loggers:
        logger.setLevel(min(logger.getEffectiveLevel(), logging.INFO))
        # A fileConfig() elsewhere in the process (e.g. Alembic) disables existing loggers; the
        # truncation report would then silently vanish.
        logger.disabled = False
        logger.addHandler(observer)
    try:
        yield
    finally:
        for logger, (level, disabled) in zip(loggers, previous, strict=True):
            logger.removeHandler(observer)
            logger.setLevel(level)
            logger.disabled = disabled
