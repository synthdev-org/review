"""Integration tests for docs/specs/spec-review-quality.md RQ1-RQ3.

The real ``adapter.llm.litellm_client.LiteLLMClient`` runs through the real
``ReviewGenerationStep`` and ``VerificationStep`` over a respx mock
(``review.eval.cassette.record``). **The canned completions are synthetic**: no
live endpoint was available, and each test writes down what the model is
assumed to return. The committed eval cassettes
(``review/testdata/eval/cassettes/v2.json``) are the bulk replay; these
tests pin the story-level behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.llm.litellm_client import LiteLLMClient
from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.eval.cassette import Interaction, record
from review.review_generation import ReviewGenerationStep
from review.step import StepContext
from review.verification import VerificationStep

pytestmark = pytest.mark.integration

_DIFF = Diff(files=(DiffFile(path="a.py", status="modified", patch="@@ -1 +1 @@\n-a\n+b\n"),))


@dataclass
class _Upstream:
    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 1
    head_sha: str = "deadbeef"


class _Redactor:
    async def redact(self, text: str) -> str:
        return text


@pytest.fixture
def reader(_reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:  # noqa: F811
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _dropped(reader: InMemoryMetricReader) -> dict[object, float]:
    data = reader.get_metrics_data()
    assert data is not None
    out: dict[object, float] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == "review.generation.dropped_total":
                    assert isinstance(metric.data, Sum)
                    for p in metric.data.data_points:
                        out[(p.attributes or {}).get("reason")] = p.value
    return out


def _completion(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(content)},
                "finish_reason": "stop",
            }
        ],
    }


def _responder(generation: dict[str, Any], verdicts: dict[str, Any] | None = None) -> Any:
    async def produce(request: httpx.Request) -> tuple[int, dict[str, Any]]:
        system = json.loads(request.content)["messages"][0]["content"]
        verifying = system.startswith("You are an automated code review verification")
        return 200, _completion(verdicts if verifying and verdicts else generation)

    return produce


def _steps(*, smell: bool) -> tuple[ReviewGenerationStep, VerificationStep]:
    client = LiteLLMClient(endpoint="https://eval.invalid/v1", api_key="placeholder")
    common = {"model": "m", "timeout": 10.0, "max_diff_bytes": 10_000}
    return (
        ReviewGenerationStep(
            client,
            redactor=_Redactor(),
            smell_baseline_enabled=smell,
            **common,  # type: ignore[arg-type]
        ),
        VerificationStep(client, **common),  # type: ignore[arg-type]
    )


def _finding(**overrides: object) -> dict[str, object]:
    return {"file": "a.py", "line": 1, "severity": "medium", "message": "m", **overrides}


async def _review(
    *, v2: bool, smell: bool, generation: dict[str, Any], verdicts: dict[str, Any] | None = None
) -> tuple[list[Finding], list[Interaction]]:
    generation_step, verification_step = _steps(smell=smell)
    context = StepContext(job_id="j", review_quality_v2=v2)
    interactions: list[Interaction] = []
    with record(_responder(generation, verdicts), interactions):
        generated = await generation_step.run(_Upstream(diff=_DIFF), context)
        verified = await verification_step.run(
            _Upstream(diff=_DIFF, findings=generated.findings), context
        )
    return verified.findings, interactions


def _verdict(index: int, **overrides: bool) -> dict[str, Any]:
    return {
        "index": index,
        "valid": True,
        "actionable": True,
        "specific": True,
        "valuable": True,
        "not_style_only": True,
        **overrides,
    }


@pytest.mark.asyncio
async def test_rq1_v2_drops_a_medium_finding_without_a_scenario_and_counts_it(
    reader: InMemoryMetricReader,
) -> None:
    # Synthetic model output: one unexplained medium, one explained medium, one bare low.
    generation = {
        "findings": [
            _finding(line=1, message="unexplained"),
            _finding(
                line=2,
                message="explained",
                scenario="Retry after a timeout replays it.",
                tier="should_fix",
            ),
            _finding(line=3, severity="low", message="bare low", tier="nit"),
        ]
    }
    verdicts = {"verdicts": [_verdict(0), _verdict(1)]}

    kept, _ = await _review(v2=True, smell=False, generation=generation, verdicts=verdicts)

    assert sorted(f.message for f in kept) == ["bare low", "explained"]
    assert _dropped(reader) == {"missing_scenario": 1}


@pytest.mark.asyncio
async def test_rq1_v1_applies_no_scenario_rule_and_sends_the_v1_prompt() -> None:
    generation = {"findings": [_finding(message="unexplained")]}
    verdicts = {"verdicts": [_verdict(0)]}

    kept, interactions = await _review(
        v2=False, smell=False, generation=generation, verdicts=verdicts
    )

    assert [f.message for f in kept] == ["unexplained"]
    assert all(f.scenario is None and f.tier is None for f in kept)
    sent = json.dumps([i.request for i in interactions])
    assert "scenario" not in sent and "not_style_only" not in sent


@pytest.mark.asyncio
async def test_rq2_v2_prompt_carries_the_reachability_rubric_and_severities_pass_through() -> None:
    # Synthetic model output following the rubric: lint-class at low, reachable defect at high.
    generation = {
        "findings": [
            _finding(line=1, severity="low", message="non-null assertion", tier="nit"),
            _finding(
                line=2,
                severity="high",
                message="restore throws when the fallback was dropped",
                scenario="A restore with no cached fallback raises KeyError.",
                tier="blocker",
            ),
        ]
    }
    verdicts = {"verdicts": [_verdict(0), _verdict(1)]}

    kept, interactions = await _review(
        v2=True, smell=False, generation=generation, verdicts=verdicts
    )

    system = interactions[0].request["messages"][0]["content"]
    assert "never above low for lint-class" in system
    assert sorted((f.severity, f.tier) for f in kept) == [("high", "blocker"), ("low", "nit")]


@pytest.mark.asyncio
async def test_rq3_no_baseline_forbids_style_and_rejects_a_slipped_one() -> None:
    # Synthetic model output: it ignores the prohibition; the judge flags it style-only.
    generation = {
        "findings": [
            _finding(line=1, severity="low", message="too many parameters", tier="nit"),
            _finding(line=2, severity="low", message="real defect", tier="should_fix"),
        ]
    }
    verdicts = {"verdicts": [_verdict(0, not_style_only=False), _verdict(1)]}

    kept, interactions = await _review(
        v2=True, smell=False, generation=generation, verdicts=verdicts
    )

    assert "Do not give style" in interactions[0].request["messages"][0]["content"]
    assert [f.message for f in kept] == ["real defect"]


@pytest.mark.asyncio
async def test_rq3_with_the_baseline_a_smell_is_posted_as_today() -> None:
    generation = {
        "findings": [_finding(severity="low", message="Possible Feature Envy: x", category="smell")]
    }
    verdicts = {"verdicts": [_verdict(0)]}

    kept, interactions = await _review(
        v2=True, smell=True, generation=generation, verdicts=verdicts
    )

    assert "Do not give style" not in interactions[0].request["messages"][0]["content"]
    assert [(f.category, f.severity) for f in kept] == [("smell", "low")]
