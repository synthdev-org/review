"""Unit tests for llmgateway's pydantic boundary models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llmgateway.models import CompletionRequest, Message


def test_completion_request_requires_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        CompletionRequest(model="local-model", messages=[])


def test_completion_request_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        CompletionRequest(
            model="local-model",
            messages=[{"role": "not-a-real-role", "content": "hi"}],
        )


def test_completion_request_accepts_minimal_valid_input() -> None:
    request = CompletionRequest(
        model="local-model",
        messages=[Message(role="user", content="hi")],
    )

    assert request.model == "local-model"
    assert request.messages[0].content == "hi"
    assert request.max_tokens is None
