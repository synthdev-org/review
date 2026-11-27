"""review.llm_response — shared "build a single-turn LLM completion
request" and "parse a batched LLM JSON envelope, tolerating malformed
items" helpers.

Extracted once ``review.intent_summary``, ``review.review_generation``,
and ``review.verification`` all needed the identical two-message
``CompletionRequest`` shape (``build_completion_request``), and once
``review.review_generation`` (``_parse_llm_findings``) and
``review.verification`` (``_parse_verdicts``) needed the identical
envelope-parse-with-per-item-tolerance pattern (``parse_llm_json_envelope``)
— this codebase's own established precedent for exactly this situation (see
``review.diff_prompt``'s own module docstring, and
``findings/redact.py``'s ``redact_findings``/``apply_redaction_ceiling``).

Zero infra imports, per architecture.md's domain-package rule — the
``llmgateway`` package is this codebase's own domain-level LLM Gateway
client, not a third-party infra dependency, and every LLM-calling step
already imports it directly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from llmgateway.models import CompletionRequest, Message


def build_completion_request(
    model: str, *, system_content: str, user_content: str
) -> CompletionRequest:
    """Builds the single system-message-plus-user-message
    ``CompletionRequest`` every LLM-calling step
    (``review.intent_summary.IntentSummaryStep``,
    ``review.review_generation.ReviewGenerationStep``,
    ``review.verification.VerificationStep``) sends to
    ``LLMClient.complete`` — the one shared request shape, extracted here
    rather than left as three independently hand-built copies of the same
    two-``Message`` list.
    """
    return CompletionRequest(
        model=model,
        messages=[
            Message(role="system", content=system_content),
            Message(role="user", content=user_content),
        ],
    )


@dataclass(frozen=True)
class EnvelopeLabels:
    """How a step names itself and its items in parse-failure log records."""

    step_name: str
    item_label: str
    item_index_field: str


def parse_llm_json_envelope[ItemT: BaseModel](
    content: str,
    *,
    envelope_key: str,
    item_model: type[ItemT],
    make_parse_error: Callable[[str], Exception],
    logger: logging.Logger,
    job_id: str,
    labels: EnvelopeLabels,
    escalate_if_all_dropped: bool = False,
) -> list[ItemT]:
    """Parses ``content`` against a ``{"<envelope_key>": [...]}`` envelope,
    raising ``make_parse_error(detail)`` on a malformed response (not JSON, or
    no top-level ``envelope_key`` list).

    One malformed item never fails the response: each item is validated
    independently, and a bad one is dropped and logged at ``warning`` via the
    caller's ``logger``. ``escalate_if_all_dropped`` also logs at ``error``
    when every item of a non-empty list failed validation.
    """
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise make_parse_error(str(exc)) from exc

    if not isinstance(raw, dict) or not isinstance(raw.get(envelope_key), list):
        raise make_parse_error(
            f"response is not a JSON object with a top-level '{envelope_key}' list"
        )

    raw_items = raw[envelope_key]
    items: list[ItemT] = []
    for index, raw_item in enumerate(raw_items):
        try:
            items.append(item_model.model_validate(raw_item))
        except (ValidationError, TypeError) as exc:
            logger.warning(
                f"{labels.step_name}: dropping one malformed {labels.item_label} "
                "from the LLM response; "
                "the rest of the response is unaffected",
                extra={"job_id": job_id, labels.item_index_field: index, "error": str(exc)},
            )

    if escalate_if_all_dropped and raw_items and not items:
        logger.error(
            f"{labels.step_name}: every {labels.item_label} in the LLM response failed validation; "
            "returning an empty list",
            extra={"job_id": job_id, "total_items": len(raw_items)},
        )
    return items
