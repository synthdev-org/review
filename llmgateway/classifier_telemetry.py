"""llmgateway.classifier_telemetry — the classifier metrics recorder
(spec-classifier US-C8, ADR-0014).

A separate domain module because adapters cannot import
`review/llm_telemetry.py` (architecture.md) and `review/_telemetry.py` is
review-package-local. OTel API only, per ADR-0011; no SDK or exporter here.

Every attribute is a closed-set label. State, questions, answers' content, org
ids, models and endpoints are never attributes. `criterion` is a caller-supplied
fixed rubric name, never free text.

Each recorder runs inside `suppress_telemetry_errors` (ADR-0012, scope extended
to this module): a recording failure is logged and never changes a classifier
call's outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from opentelemetry import metrics

from llmgateway.classifier import ClassifierUsage, Decision

logger = logging.getLogger(__name__)

CallOutcome = Literal["ok", "unauthorized", "unreachable", "malformed", "rate_limited"]
ShadowOutcome = Literal["agree", "disagree", "uncertain", "error", "timeout"]
FallbackReason = Literal[
    "uncertain",
    "error",
    "high_severity_no",
    "spec_category",
    "redaction_failure",
    "credential_unresolved",
]

InFlightResource = Literal["classifier", "redaction"]

_meter = metrics.get_meter("review.classifier")
_calls = _meter.create_counter(
    "review.classifier.calls_total", description="Classifier calls by backend and outcome."
)
_duration = _meter.create_histogram(
    "review.classifier.call_duration_ms", unit="ms", description="Classifier call duration."
)
_tokens = _meter.create_counter(
    "review.classifier.tokens_total", description="Classifier token usage."
)
_verdicts = _meter.create_counter(
    "review.classifier.verdict_total", description="Thresholded verdicts per rubric criterion."
)
_shadow = _meter.create_counter(
    "review.classifier.shadow_agreement_total",
    description="Shadow-mode agreement with the LLM judge per criterion.",
)
_fallbacks = _meter.create_counter(
    "review.verification.llm_fallback_total",
    description="Findings sent to the LLM judge instead of the classifier.",
)
_in_flight = _meter.create_up_down_counter(
    "review.classifier.in_flight",
    description="Classifier calls and redaction scans in flight on this worker.",
)


@contextmanager
def suppress_telemetry_errors(message: str) -> Iterator[None]:
    """Local copy of the ADR-0012 pattern (the other copies are not importable
    from `llmgateway`): log at warning and continue.
    """
    try:
        yield
    except Exception:
        logger.warning(message, exc_info=True)


def record_call(*, backend: str, outcome: CallOutcome, duration_ms: float) -> None:
    with suppress_telemetry_errors("classifier call telemetry failed (call unaffected)"):
        attrs = {"backend": backend, "outcome": outcome}
        _calls.add(1, attrs)
        _duration.record(duration_ms, attrs)


def record_token_usage(usage: ClassifierUsage, *, backend: str) -> None:
    with suppress_telemetry_errors("classifier token telemetry failed (call unaffected)"):
        _tokens.add(usage.input_tokens, {"backend": backend, "token_type": "input"})  # nosec B105 - metric label
        _tokens.add(usage.output_tokens, {"backend": backend, "token_type": "output"})  # nosec B105 - metric label


def record_verdict(*, criterion: str, decision: Decision) -> None:
    with suppress_telemetry_errors("classifier verdict telemetry failed (review unaffected)"):
        _verdicts.add(1, {"criterion": criterion, "decision": decision})


def record_shadow_agreement(*, criterion: str, outcome: ShadowOutcome) -> None:
    with suppress_telemetry_errors("classifier shadow telemetry failed (review unaffected)"):
        _shadow.add(1, {"criterion": criterion, "outcome": outcome})


def record_llm_fallback(*, reason: FallbackReason) -> None:
    with suppress_telemetry_errors("classifier fallback telemetry failed (review unaffected)"):
        _fallbacks.add(1, {"reason": reason})


def record_in_flight(resource: InFlightResource, delta: int) -> None:
    with suppress_telemetry_errors("classifier in-flight telemetry failed (review unaffected)"):
        _in_flight.add(delta, {"resource": resource})
