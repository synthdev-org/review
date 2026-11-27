"""Shared helpers for the `FindingJudge` tests (no tests of its own).

The classifier side is the real `TypeSafeClassifier` over the egress guard, with
the wire replayed through `httpx2.MockTransport` (the guard's `inner` seam; respx
patches `httpx` only, see adapter/classifier/test_typesafe.py). Response bodies
have the shape of `adapter/classifier/testdata/jev-live-wire-examples.json`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import llmgateway.classifier_telemetry as tel
import review.llm_telemetry as llm_tel
from adapter.classifier.test_typesafe import make
from adapter.classifier.typesafe import TypeSafeClassifier
from findings.models import Finding, Severity
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from review.finding_judge import (
    ClassifierSelection,
    ClassifierVerification,
    Mode,
)
from review.step import StepContext
from review.verification import VerificationStep

PATCH = "@@ -1,2 +1,3 @@\n a\n+b = 1\n c\n@@ -40,2 +41,3 @@\n x\n+danger = 2\n y\n"
DIFF = Diff(files=(DiffFile(path="app.py", status="modified", patch=PATCH),))
CRITERIA = ("valid", "actionable", "specific", "valuable")


def finding(
    index: int = 0,
    *,
    severity: Severity = "medium",
    message: str | None = None,
    line: int = 2,
) -> Finding:
    return Finding(
        file="app.py",
        line=line,
        severity=severity,
        message=message or f"llm finding {index}",
        source="llm",
    )


@dataclass
class UpstreamOutput:
    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"


@dataclass
class FakeLLM:
    """An `LLMClient` fake: passes every finding unless `fail_indexes` names it."""

    fail_indexes: frozenset[int] = frozenset()
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        asked = json.loads(request.messages[1].content.split("\n", 1)[1].split("\n\n", 1)[0])
        verdicts = [
            {
                "index": item["index"],
                **dict.fromkeys(CRITERIA, item["index"] not in self.fail_indexes),
            }
            for item in asked
        ]
        return CompletionResponse(content=json.dumps({"verdicts": verdicts}), model=request.model)


class MaskingRedactor:
    """Redacts only whole `AKIA...` tokens, like a real ruleset: a secret cut in
    half before scanning would not match and would leak."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def redact(self, text: str) -> str:
        import re

        self.seen.append(text)
        return re.sub(r"AKIA[A-Z0-9]{16}", "[REDACTED]", text)


def wire(
    probabilities: Callable[[str, str], float],
    requests: list[dict[str, Any]] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    """A TypeSafe wire handler: answers each asked question with
    `probabilities(state, question_id)`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if requests is not None:
            requests.append(body)
        return httpx2.Response(
            200,
            json={
                "model": body["model"],
                "answers": {
                    q: {"type": "noul", "noul": probabilities(body["state"], q)}
                    for q in body["questions"]
                },
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        )

    return handler


class Resolver:
    """A per-job `ClassifierSelectionResolver` fake: records the orgs it was asked about."""

    def __init__(self, selections: dict[str, ClassifierSelection | None]) -> None:
        self._selections = selections
        self.asked: list[str] = []

    async def resolve(self, org_id: str) -> ClassifierSelection | None:
        self.asked.append(org_id)
        return self._selections.get(org_id)


def selection(classifier: TypeSafeClassifier | None, mode: Mode) -> ClassifierSelection:
    return ClassifierSelection(mode=mode, model="jev-1.14.0", classifier=classifier)


def wiring(
    resolver: Resolver,
    *,
    redactor: MaskingRedactor | None = None,
    max_state_bytes: int = 4000,
    slots: int = 4,
) -> ClassifierVerification:
    return ClassifierVerification(
        resolver=resolver,
        redactor=redactor or MaskingRedactor(),
        redaction_slots=asyncio.Semaphore(slots),
        classifier_slots=asyncio.Semaphore(slots),
        max_state_bytes=max_state_bytes,
        timeout=5.0,
    )


def step_with(llm: FakeLLM, classifier: ClassifierVerification | None) -> VerificationStep:
    return VerificationStep(
        llm, model="m", timeout=5.0, max_diff_bytes=100_000, classifier=classifier
    )


async def run_step(
    step: VerificationStep, findings: Sequence[Finding], *, org_id: str = "org-1"
) -> list[Finding]:
    out = await step.run(
        UpstreamOutput(diff=DIFF, findings=list(findings), org_id=org_id), StepContext(job_id="j1")
    )
    return list(out.findings)


def classifier_for(
    probabilities: Callable[[str, str], float],
    requests: list[dict[str, Any]] | None = None,
) -> TypeSafeClassifier:
    return make(wire(probabilities, requests))


@pytest.fixture
def points(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[], dict[str, list[dict[str, Any]]]]]:
    """Swaps the classifier instruments for ones an in-memory reader can see and
    returns a function giving `{metric name: [attribute dicts]}`."""
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    monkeypatch.setattr(tel, "_verdicts", meter.create_counter("review.classifier.verdict_total"))
    monkeypatch.setattr(
        tel, "_shadow", meter.create_counter("review.classifier.shadow_agreement_total")
    )
    monkeypatch.setattr(
        tel, "_fallbacks", meter.create_counter("review.verification.llm_fallback_total")
    )
    monkeypatch.setattr(
        llm_tel,
        "_verdict_field_missing_counter",
        meter.create_counter("review.verification.verdict_field_missing_total"),
    )
    monkeypatch.setattr(
        tel, "_in_flight", meter.create_up_down_counter("review.classifier.in_flight")
    )

    def read() -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        data = reader.get_metrics_data()
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    out[metric.name] = [
                        {**dict(p.attributes), "_value": p.value} for p in metric.data.data_points
                    ]
        return out

    yield read
