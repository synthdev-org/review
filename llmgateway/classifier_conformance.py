"""llmgateway.classifier_conformance — the contract every `Classifier` backend
(the in-memory fake, TypeSafe, any later one) must pass (ADR-0014, US-C7).

`CHECKS` are async callables taking a classifier; each raises
`ConformanceFailure` with a content-free message. The suite asserts only what
holds for any backend with any answers; it does not assume particular
probabilities.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from llmgateway.classifier import ClassificationRequest, Classifier, NoulQuestion

MODEL = "conformance-model"
TIMEOUT = 10.0
_STATE = "DIFF:\n-    x = 1\n+    x = 2\n\nFINDING: x changed."


class ConformanceFailure(AssertionError):
    pass


def _request(*ids: str) -> ClassificationRequest:
    return ClassificationRequest(
        model=MODEL,
        state=_STATE,
        questions={i: NoulQuestion(instructions=f"Question {i}?") for i in ids},
    )


async def _answers_keyed_by_question_ids(classifier: Classifier) -> None:
    for ids in (("a",), ("valid", "actionable", "has_scenario", "in_scope")):
        response = await classifier.classify(_request(*ids), timeout=TIMEOUT)
        if set(response.answers) != set(ids):
            raise ConformanceFailure(f"answers keys differ from the {len(ids)} question ids")


async def _answers_are_probabilities(classifier: Classifier) -> None:
    response = await classifier.classify(_request("a", "b"), timeout=TIMEOUT)
    for answer in response.answers.values():
        if not 0.0 <= answer.p <= 1.0:
            raise ConformanceFailure("an answer probability is outside [0, 1]")


async def _repeat_call_is_independent(classifier: Classifier) -> None:
    # A backend must not carry questions over from a previous call.
    await classifier.classify(_request("first"), timeout=TIMEOUT)
    response = await classifier.classify(_request("second"), timeout=TIMEOUT)
    if set(response.answers) != {"second"}:
        raise ConformanceFailure("a later call returned answers for another call's questions")


CHECKS: dict[str, Callable[[Classifier], Awaitable[None]]] = {
    "answers_keyed_by_question_ids": _answers_keyed_by_question_ids,
    "answers_are_probabilities": _answers_are_probabilities,
    "repeat_call_is_independent": _repeat_call_is_independent,
}
