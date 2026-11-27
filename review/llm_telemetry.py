"""review.llm_telemetry — the shared US3 LLM token-usage recorder every
pipeline stage that makes its own completion call (``review_generation.py``,
``verification.py``) uses, so the `{stage, model, token_type}` recording
shape (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) can't drift
between stages the way two independently hand-maintained copies would.

Owns the shared ``review.llm`` meter/counter both stages record against, per
the catalog's own instruction that every stage's LLM spend rolls up into one
metric distinguished only by the ``stage`` attribute — recorded here (not in
``adapter/llm/litellm_client.py``) because the adapter itself stays
pipeline-stage-unaware, per that same instruction.

Imports only the OTel API (``opentelemetry.metrics``), the same
``meter = metrics.get_meter(...)`` style established across this codebase —
no SDK/exporter construction here, that stays exclusively at the composition
root, per ADR-0011.
"""

from __future__ import annotations

import logging
from typing import Literal

from opentelemetry import metrics

from llmgateway.models import TokenUsage
from review._telemetry import suppress_telemetry_errors

meter = metrics.get_meter("review.llm")

# Catalog name, verbatim. Attribute values are strictly {stage, model,
# token_type} plus the integer token count itself — counts only, never
# source code/diff content/finding text, per the Privacy/Compliance NFR.
_llm_tokens_counter = meter.create_counter(
    "review.llm.tokens_total",
    description="LLM token usage (prompt/completion/total) recorded per completion call.",
)

VerdictField = Literal["not_style_only"]

_verdict_field_missing_counter = meter.create_counter(
    "review.verification.verdict_field_missing_total",
    description="LLM judge verdicts that omitted a required rubric field (defaulted).",
)


def record_verdict_field_missing(field: VerdictField, *, logger: logging.Logger) -> None:
    """Counts one verdict that omitted ``field``; ``field`` is a closed set."""
    with suppress_telemetry_errors(
        logger, "verdict-field-missing telemetry recording failed (verification unaffected)"
    ):
        _verdict_field_missing_counter.add(1, attributes={"field": field})


def record_llm_token_usage(
    usage: TokenUsage, *, model: str, stage: str, logger: logging.Logger
) -> None:
    """Records US3's three token counters right after a completion call
    returns a response with ``usage`` set — three ``.add()`` calls, one
    each for prompt/completion/total, tagged by the caller's own fixed
    ``stage`` name (a closed-set label value, never free text, per the
    catalog's Scale NFR cardinality bound) and the response's own model
    (never a free-text value).

    Args:
        usage: the completion response's own token counts.
        model: the response's own model identifier.
        stage: the calling pipeline stage's fixed name (e.g.
            ``"review-generation"``, ``"verification"``).
        logger: the caller's own module logger, so a recording failure logs
            under the calling stage's module name.

    """
    with suppress_telemetry_errors(
        logger, f"LLM token usage telemetry recording failed ({stage} unaffected)"
    ):
        for token_type, value in (
            ("prompt", usage.prompt_tokens),
            ("completion", usage.completion_tokens),
            ("total", usage.total_tokens),
        ):
            _llm_tokens_counter.add(
                value,
                attributes={"stage": stage, "model": model, "token_type": token_type},
            )
