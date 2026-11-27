"""Tests for review.llm_response.build_completion_request/
parse_llm_json_envelope, per docs/rules/testing.md's "Unit tests co-located"
convention — this module's own two consumers (review/review_generation.py's
``_parse_llm_findings``, review/verification.py's ``_parse_verdicts``) each
exercise it indirectly via their own step-level tests, but neither proves
this shared module's own contract (e.g. the ``escalate_if_all_dropped``
distinction one consumer sets and the other doesn't) independently of
whichever consumer happens to reach a given code path.

Pure-function unit tests, no I/O, per testing.md's Unit tier.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import BaseModel, ConfigDict

from review.llm_response import (
    EnvelopeLabels,
    build_completion_request,
    parse_llm_json_envelope,
)

# --- build_completion_request -------------------------------------------------


def test_build_completion_request_builds_one_system_and_one_user_message() -> None:
    request = build_completion_request(
        "gpt-test", system_content="you are a reviewer", user_content="review this diff"
    )

    assert request.model == "gpt-test"
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[0].content == "you are a reviewer"
    assert request.messages[1].content == "review this diff"


# --- parse_llm_json_envelope --------------------------------------------------


class _Item(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    value: str


class _ParseError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _parse(content: str, *, escalate_if_all_dropped: bool = False) -> list[_Item]:
    return parse_llm_json_envelope(
        content,
        envelope_key="items",
        item_model=_Item,
        make_parse_error=_ParseError,
        logger=logging.getLogger("test"),
        job_id="job-1",
        labels=EnvelopeLabels(
            step_name="test-step",
            item_label="item",
            item_index_field="item_index",
        ),
        escalate_if_all_dropped=escalate_if_all_dropped,
    )


def test_parses_every_well_formed_item() -> None:
    content = json.dumps({"items": [{"index": 0, "value": "a"}, {"index": 1, "value": "b"}]})

    items = _parse(content)

    assert [item.value for item in items] == ["a", "b"]


def test_raises_make_parse_error_on_invalid_json() -> None:
    with pytest.raises(_ParseError):
        _parse("not json")


def test_raises_make_parse_error_when_envelope_key_missing() -> None:
    with pytest.raises(_ParseError):
        _parse(json.dumps({"wrong_key": []}))


def test_raises_make_parse_error_when_envelope_key_not_a_list() -> None:
    with pytest.raises(_ParseError):
        _parse(json.dumps({"items": "not a list"}))


def test_drops_one_malformed_item_and_keeps_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps(
        {
            "items": [
                {"index": 0, "value": "a"},
                {"index": 1},  # missing required "value"
                {"index": 2, "value": "c"},
            ]
        }
    )

    with caplog.at_level(logging.WARNING):
        items = _parse(content)

    assert [item.value for item in items] == ["a", "c"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].item_index == 1  # type: ignore[attr-defined]


def test_empty_envelope_list_returns_empty_with_no_escalation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        items = _parse(json.dumps({"items": []}), escalate_if_all_dropped=True)

    assert items == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_all_items_dropped_escalates_to_error_when_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps({"items": [{"index": 0}, {"index": 1}]})

    with caplog.at_level(logging.WARNING):
        items = _parse(content, escalate_if_all_dropped=True)

    assert items == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


def test_all_items_dropped_does_not_escalate_when_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps({"items": [{"index": 0}, {"index": 1}]})

    with caplog.at_level(logging.WARNING):
        items = _parse(content, escalate_if_all_dropped=False)

    assert items == []
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
