"""Unit tests for docs/specs/spec-review-quality.md RQ1-RQ3, RQ7: the v2 prompt
behind ``StepContext.review_quality_v2``, the scenario/tier parse rules, the
``not_style_only`` verification criterion and ``scenario`` redaction. Fake
``LLMClient``/redactor only; the integration counterpart is
``review/test_review_quality_prompt.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from apps.worker.settings import WorkerSettings
from findings.models import Finding
from findings.redact import redact_findings
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from review.review_generation import (
    _SYSTEM_PROMPT_PREFIX,
    _SYSTEM_PROMPT_SUFFIX_HEAD,
    _SYSTEM_PROMPT_SUFFIX_TAIL,
    ReviewGenerationStep,
    _build_system_prompt,
    _parse_llm_findings,
)
from review.step import StepContext
from review.verification import (
    _SYSTEM_PROMPT,
    _SYSTEM_PROMPT_V2,
    VerificationStep,
    _VerificationVerdictItem,
)


@pytest.fixture
def reader(_reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:  # noqa: F811
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _counts(reader: InMemoryMetricReader, name: str, attr: str) -> dict[object, float]:
    data = reader.get_metrics_data()
    counts: dict[object, float] = {}
    if data is None:
        return counts
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    assert isinstance(metric.data, Sum)
                    for point in metric.data.data_points:
                        counts[(point.attributes or {}).get(attr)] = point.value
    return counts


def _item(**overrides: object) -> dict[str, object]:
    return {"file": "a.py", "line": 1, "severity": "medium", "message": "m", **overrides}


def _parse(items: list[dict[str, object]], *, v2: bool, smell: bool = False) -> list[Finding]:
    return _parse_llm_findings(
        json.dumps({"findings": items}), job_id="j", smell_baseline=smell, v2=v2
    )


# --- prompt -----------------------------------------------------------------


def test_v1_prompt_is_unchanged_when_the_v2_flag_is_off() -> None:
    assert _build_system_prompt(None, smell_baseline=False) == (
        _SYSTEM_PROMPT_PREFIX + _SYSTEM_PROMPT_SUFFIX_HEAD + _SYSTEM_PROMPT_SUFFIX_TAIL
    )
    assert _build_system_prompt(None, smell_baseline=False, v2=False) == _build_system_prompt(
        None, smell_baseline=False
    )
    assert "scenario" not in _build_system_prompt("intent", smell_baseline=True)


@pytest.mark.parametrize("smell", [False, True])
@pytest.mark.parametrize("intent", [None, "adds retries"])
def test_v2_prompt_flag_combinations(smell: bool, intent: str | None) -> None:
    prompt = _build_system_prompt(intent, smell_baseline=smell, v2=True)

    assert '"scenario": ' in prompt and '"tier": ' in prompt
    assert "reachab" in prompt  # reachability severity rubric
    assert "Do not praise" in prompt
    # RQ3: the style prohibition exists only without the smell baseline.
    assert ("Do not give style" in prompt) is (not smell)
    assert ("12 code smells" in prompt) is smell
    assert ("What this PR is trying to do" in prompt) is (intent is not None)


# --- parse rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "kept"),
    [("critical", False), ("high", False), ("medium", False), ("low", True), ("info", True)],
)
def test_v2_drops_a_finding_without_scenario_exactly_at_the_medium_boundary(
    severity: str, kept: bool, reader: InMemoryMetricReader
) -> None:
    findings = _parse([_item(severity=severity)], v2=True)

    assert (len(findings) == 1) is kept
    dropped = _counts(reader, "review.generation.dropped_total", "reason")
    assert dropped.get("missing_scenario", 0) == (0 if kept else 1)


def test_v2_treats_a_blank_scenario_as_missing_and_keeps_a_stripped_one() -> None:
    findings = _parse(
        [_item(scenario="   "), _item(line=2, scenario="  retry after timeout  ")], v2=True
    )

    assert [f.scenario for f in findings] == ["retry after timeout"]


def test_v2_smell_clamped_to_low_needs_no_scenario() -> None:
    findings = _parse([_item(severity="high", category="smell")], v2=True, smell=True)

    assert [(f.severity, f.scenario) for f in findings] == [("low", None)]


def test_v1_applies_neither_rule_and_ignores_scenario_and_tier() -> None:
    findings = _parse([_item(scenario="s", tier="nit")], v2=False)

    assert [(f.scenario, f.tier) for f in findings] == [(None, None)]


def test_v2_unknown_tier_becomes_none_and_is_counted_but_the_finding_is_kept(
    reader: InMemoryMetricReader,
) -> None:
    findings = _parse(
        [
            _item(scenario="s", tier="blocker"),
            _item(line=2, scenario="s", tier="urgent"),
            _item(line=3, scenario="s"),
        ],
        v2=True,
    )

    assert [f.tier for f in findings] == ["blocker", None, None]
    assert _counts(reader, "review.generation.dropped_total", "reason") == {"tier_invalid": 1}


# --- job snapshot -----------------------------------------------------------


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


@dataclass
class _FakeLLM:
    content: str
    calls: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(content=self.content, model=request.model, usage=None)


class _MaskingRedactor:
    async def redact(self, text: str) -> str:
        return text.replace("sk-live-1234567890", "[REDACTED]")


_DIFF = Diff(files=(DiffFile(path="a.py", status="modified", patch="@@ -1 +1 @@\n-a\n+b\n"),))


def _generation(llm: _FakeLLM) -> ReviewGenerationStep:
    return ReviewGenerationStep(
        llm,
        redactor=_MaskingRedactor(),
        model="m",
        timeout=5.0,
        max_diff_bytes=10_000,
        smell_baseline_enabled=False,
    )


def test_the_snapshot_defaults_to_v1_everywhere() -> None:
    assert StepContext(job_id="j").review_quality_v2 is False
    assert WorkerSettings.model_fields["review_quality_v2_enabled"].default is False


@pytest.mark.asyncio
async def test_each_job_uses_its_own_snapshot_not_a_shared_setting() -> None:
    """One step instance, two jobs with different snapshots: the prompt and
    parse rule follow each job's own context, never anything global."""
    llm = _FakeLLM(json.dumps({"findings": [_item()]}))
    step = _generation(llm)

    v1 = await step.run(_Upstream(diff=_DIFF), StepContext(job_id="1"))
    v2 = await step.run(_Upstream(diff=_DIFF), StepContext(job_id="2", review_quality_v2=True))
    v1_again = await step.run(_Upstream(diff=_DIFF), StepContext(job_id="3"))

    assert [len(o.findings) for o in (v1, v2, v1_again)] == [1, 0, 1]
    systems = [c.messages[0].content for c in llm.calls]
    assert systems[0] == systems[2] != systems[1]


# --- redaction (RQ7) --------------------------------------------------------


@pytest.mark.asyncio
async def test_redact_findings_redacts_scenario_as_well_as_message() -> None:
    finding = Finding(
        file="a.py",
        severity="high",
        message="key sk-live-1234567890 leaks",
        source="llm",
        scenario="Calling with sk-live-1234567890 logs it",
    )
    no_scenario = finding.model_copy(update={"scenario": None})

    redacted = await redact_findings(
        [finding, no_scenario], _MaskingRedactor(), job_id="j", log_prefix="t"
    )

    assert redacted[0].message == "key [REDACTED] leaks"
    assert redacted[0].scenario == "Calling with [REDACTED] logs it"
    assert redacted[1].scenario is None


@pytest.mark.asyncio
async def test_a_secret_shaped_scenario_is_redacted_by_the_generation_step() -> None:
    llm = _FakeLLM(
        json.dumps({"findings": [_item(scenario="the token sk-live-1234567890 is logged")]})
    )

    out = await _generation(llm).run(
        _Upstream(diff=_DIFF), StepContext(job_id="j", review_quality_v2=True)
    )

    assert [f.scenario for f in out.findings] == ["the token [REDACTED] is logged"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("v2", "expected"), [(True, {"blocker"}), (False, set())])
async def test_findings_produced_has_a_tier_label_only_under_v2(
    reader: InMemoryMetricReader, v2: bool, expected: set[str]
) -> None:
    llm = _FakeLLM(json.dumps({"findings": [_item(scenario="s", tier="blocker")]}))

    await _generation(llm).run(_Upstream(diff=_DIFF), StepContext(job_id="j", review_quality_v2=v2))

    data = reader.get_metrics_data()
    assert data is not None
    attrs = [
        dict(p.attributes or {})
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.findings.produced_total"
        for p in m.data.data_points
    ]
    assert attrs == [{"category": "none", **({"tier": "blocker"} if v2 else {})}]
    assert {a["tier"] for a in attrs if "tier" in a} == expected


# --- verification criterion (A3) --------------------------------------------


def test_v2_verification_prompt_names_five_criteria_and_v1_is_unchanged() -> None:
    assert "not_style_only" in _SYSTEM_PROMPT_V2
    assert "all five of these" in _SYSTEM_PROMPT_V2
    assert '"not_style_only": <bool>}, ...]}' in _SYSTEM_PROMPT_V2
    assert "not_style_only" not in _SYSTEM_PROMPT
    assert "all four of these" in _SYSTEM_PROMPT


def test_mixed_version_verdicts_both_parse() -> None:
    v1_shaped = _VerificationVerdictItem.model_validate(
        {"index": 0, "valid": True, "actionable": True, "specific": True, "valuable": True}
    )
    v2_shaped = {
        "index": 0,
        "valid": True,
        "actionable": True,
        "specific": True,
        "valuable": True,
        "not_style_only": False,
    }
    assert v1_shaped.not_style_only is True
    assert _VerificationVerdictItem.model_validate(v2_shaped).not_style_only is False


async def _verify(*, v2: bool, verdict_extra: dict[str, Any]) -> tuple[list[Finding], _FakeLLM]:
    verdict = {
        "index": 0,
        "valid": True,
        "actionable": True,
        "specific": True,
        "valuable": True,
        **verdict_extra,
    }
    llm = _FakeLLM(json.dumps({"verdicts": [verdict]}))
    step = VerificationStep(llm, model="m", timeout=5.0, max_diff_bytes=10_000)
    finding = Finding(
        file="a.py", severity="medium", message="m", source="llm", scenario="breaks on retry"
    )
    out = await step.run(
        _Upstream(diff=_DIFF, findings=[finding]), StepContext(job_id="j", review_quality_v2=v2)
    )
    return out.findings, llm


@pytest.mark.asyncio
async def test_v2_rejects_a_style_only_verdict_and_v1_ignores_the_field() -> None:
    kept_v2, llm = await _verify(v2=True, verdict_extra={"not_style_only": False})
    kept_v1, _ = await _verify(v2=False, verdict_extra={"not_style_only": False})

    assert kept_v2 == []
    assert len(kept_v1) == 1
    assert "breaks on retry" in llm.calls[0].messages[1].content  # the judge sees the scenario


@pytest.mark.asyncio
async def test_v2_code_accepts_a_v1_shaped_verdict() -> None:
    kept, _ = await _verify(v2=True, verdict_extra={})

    assert len(kept) == 1
