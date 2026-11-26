"""In-memory `Classifier` fake. Test tooling: never in the runtime image
(see `adapter/product_knowledge/test_packaging_guards.py`)."""

from __future__ import annotations

from collections.abc import Mapping

from llmgateway.classifier import (
    ClassificationRequest,
    ClassificationResponse,
    ClassifierUsage,
    NoulAnswer,
)


class FakeClassifier:
    """Answers each question id from `probabilities`, else `default`.

    `error`, when set, is raised by every call (use an `LLMError` subclass, as a
    real backend would). `calls` records every request for assertions.
    """

    def __init__(
        self,
        probabilities: Mapping[str, float] | None = None,
        *,
        default: float = 0.5,
        error: Exception | None = None,
    ) -> None:
        self._probabilities = dict(probabilities or {})
        self._default = default
        self._error = error
        self.calls: list[ClassificationRequest] = []

    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return ClassificationResponse(
            model=request.model,
            answers={
                qid: NoulAnswer(p=self._probabilities.get(qid, self._default))
                for qid in request.questions
            },
            usage=ClassifierUsage(input_tokens=len(request.state), output_tokens=0),
        )
