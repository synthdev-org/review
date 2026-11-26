"""The conformance suite passes against the fake and fails against seeded
faults (proving each check can fail). The TypeSafe adapter runs the same
`CHECKS` in `adapter/classifier/test_conformance.py`."""

from __future__ import annotations

import pytest

from llmgateway.classifier import (
    ClassificationRequest,
    ClassificationResponse,
    Classifier,
    NoulAnswer,
)
from llmgateway.classifier_conformance import CHECKS, ConformanceFailure
from llmgateway.testing.fake_classifier import FakeClassifier


class _DropsAnAnswer:
    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        ids = list(request.questions)
        return ClassificationResponse(
            model=request.model, answers={i: NoulAnswer(p=0.5) for i in ids[:1]}
        )


class _OutOfRange:
    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        answer = NoulAnswer.model_construct(p=1.5)
        return ClassificationResponse.model_construct(
            model=request.model, answers=dict.fromkeys(request.questions, answer), usage=None
        )


class _Sticky:
    """Answers the first call's questions forever."""

    def __init__(self) -> None:
        self._ids: list[str] | None = None

    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        self._ids = self._ids or list(request.questions)
        return ClassificationResponse(
            model=request.model, answers={i: NoulAnswer(p=0.5) for i in self._ids}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CHECKS)
async def test_fake_passes_every_check(name: str) -> None:
    await CHECKS[name](FakeClassifier())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "faulty"),
    [
        ("answers_keyed_by_question_ids", _DropsAnAnswer()),
        ("answers_are_probabilities", _OutOfRange()),
        ("repeat_call_is_independent", _Sticky()),
    ],
)
async def test_each_check_fails_against_its_seeded_fault(name: str, faulty: Classifier) -> None:
    with pytest.raises(ConformanceFailure):
        await CHECKS[name](faulty)
