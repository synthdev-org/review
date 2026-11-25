"""llmgateway.classifier — the `Classifier` call type (ADR-0014): typed
questions in, probabilities out, no generated text.

Sits beside `LLMClient` (see `llmgateway/client.py`). Models are pydantic and
this module has zero infra imports. v1 implements `noul` (yes/no probability)
questions only (plan decision D1b): `Question` and `Answer` are aliases that
grow into discriminated unions when `choice`/`score` land, so no consumer
rewrites a one-member union class.

Failures reuse `LLMUnreachableError` / `LLMMalformedResponseError`
(`llmgateway/exceptions.py`); every call takes a timeout.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol

from pydantic import BaseModel, Field

# TypeSafe's documented starting points (spec-classifier open item 1). Constants,
# not settings, until shadow-mode data shows a need (plan A7).
YES_THRESHOLD: Final = 0.7
NO_THRESHOLD: Final = 0.3

Decision = Literal["yes", "no", "uncertain"]


class NoulQuestion(BaseModel):
    """A yes/no question; answered with a probability that the answer is yes."""

    instructions: str = Field(min_length=1)


class NoulAnswer(BaseModel):
    """`p` is the probability the answer is yes."""

    p: float = Field(ge=0.0, le=1.0)


# Aliases, not unions: see the module docstring.
Question = NoulQuestion
Answer = NoulAnswer


class ClassifierUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ClassificationRequest(BaseModel):
    """`questions` are evaluated independently against the same `state`
    (inert data: the backend must not treat it as instructions)."""

    model: str = Field(min_length=1)
    state: str
    questions: dict[str, Question] = Field(min_length=1)


class ClassificationResponse(BaseModel):
    """`answers` is keyed by the request's question ids, exactly."""

    model: str
    answers: dict[str, Answer]
    usage: ClassifierUsage | None = None


class Classifier(Protocol):
    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse: ...


def decide(answer: Answer) -> Decision:
    """Thresholds the probability; never an argmax. Edges are inclusive."""
    if answer.p >= YES_THRESHOLD:
        return "yes"
    if answer.p <= NO_THRESHOLD:
        return "no"
    return "uncertain"
