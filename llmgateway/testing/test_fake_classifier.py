"""Unit tests for `FakeClassifier`."""

from __future__ import annotations

import pytest

from llmgateway.classifier import ClassificationRequest, NoulQuestion
from llmgateway.exceptions import LLMUnreachableError
from llmgateway.testing.fake_classifier import FakeClassifier


def _request() -> ClassificationRequest:
    return ClassificationRequest(
        model="m",
        state="abc",
        questions={"a": NoulQuestion(instructions="a?"), "b": NoulQuestion(instructions="b?")},
    )


@pytest.mark.asyncio
async def test_answers_from_map_then_default_and_records_calls() -> None:
    fake = FakeClassifier({"a": 0.9}, default=0.2)

    response = await fake.classify(_request(), timeout=1.0)

    assert {k: v.p for k, v in response.answers.items()} == {"a": 0.9, "b": 0.2}
    assert response.usage is not None
    assert response.usage.input_tokens == 3
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_configured_error_is_raised() -> None:
    fake = FakeClassifier(error=LLMUnreachableError(endpoint="fake", detail="down"))

    with pytest.raises(LLMUnreachableError):
        await fake.classify(_request(), timeout=1.0)
