"""Unit tests for the `Classifier` models and threshold helper."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llmgateway.classifier import (
    ClassificationRequest,
    NoulAnswer,
    NoulQuestion,
    decide,
)


@pytest.mark.parametrize(
    ("p", "expected"),
    [
        (1.0, "yes"),
        (0.7, "yes"),
        (0.69, "uncertain"),
        (0.5, "uncertain"),
        (0.31, "uncertain"),
        (0.3, "no"),
        (0.0, "no"),
    ],
)
def test_decide_thresholds_with_inclusive_edges(p: float, expected: str) -> None:
    assert decide(NoulAnswer(p=p)) == expected


@pytest.mark.parametrize("p", [-0.01, 1.01])
def test_answer_probability_must_be_in_unit_interval(p: float) -> None:
    with pytest.raises(ValidationError):
        NoulAnswer(p=p)


def test_request_requires_at_least_one_question() -> None:
    with pytest.raises(ValidationError):
        ClassificationRequest(model="m", state="s", questions={})


def test_request_accepts_a_noul_question() -> None:
    request = ClassificationRequest(
        model="m", state="s", questions={"valid": NoulQuestion(instructions="Is it?")}
    )
    assert list(request.questions) == ["valid"]
