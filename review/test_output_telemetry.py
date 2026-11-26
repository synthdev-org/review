"""Unit tests for US4's findings-count telemetry, recorded at
``review.output``'s ``merge_findings`` call site (not inside
``findings/merge.py`` itself — that module stays a pure computation, per
its own docstring). Uses OTel's own in-memory metric reader (no I/O, per
docs/rules/testing.md's Unit tier) and a minimal fake ``FindingsPublisher``
so ``FindingsOutputStep.run`` is exercised directly, without the real
GitHub-posting adapter/network stub the co-located
``review/test_findings_output_step.py`` integration tests use.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from adapter.telemetry.conftest import (
    reset_otel_global_providers as _reset_otel_global_providers,  # noqa: F401
)
from findings.models import Finding, Severity, Source
from review.context_bundle import ContextBundle, ContextBundleItem, ContextItemKind
from review.output import FindingsOutputResult, FindingsOutputStep
from review.step import StepContext

pytestmark = pytest.mark.asyncio

_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_OWNER_REPO = "acme/widgets"


class _FakeFindingsPublisher:
    """Satisfies ``review.output.FindingsPublisher`` without any real I/O —
    every call just records that it happened, for assertions unrelated to
    telemetry (none of these tests need to assert on posting behavior, but
    ``FindingsOutputStep.run`` always calls ``post_check_run`` on its happy
    path, so a working fake is required regardless)."""

    def __init__(self) -> None:
        self.posted_reviews: list[list[Finding]] = []
        self.posted_check_run_counts: list[int] = []

    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        findings: list[Finding],
        collapsed_findings: list[Finding] | None = None,
        unreviewed_files: tuple[str, ...] = (),
    ) -> None:
        self.posted_reviews.append(findings)

    async def post_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        finding_count: int,
    ) -> None:
        self.posted_check_run_counts.append(finding_count)

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        raise AssertionError("not exercised by these tests")


def _finding(*, file: str, line: int, severity: Severity, source: Source, message: str) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        message=message,
        source=source,
    )


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real ``MeterProvider`` backed by ``InMemoryMetricReader``
    as the global one — see ``adapter/telemetry/test_heartbeat.py``'s
    identical fixture docstring for why this is required for
    ``review.output``'s module-level ``meter``/counters (created once, at
    import time) to pick this test's provider up via OTel's proxy-instrument
    migration mechanism."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _metric_data_points(
    reader: InMemoryMetricReader, metric_name: str
) -> list[tuple[float, dict[str, object]]]:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    points: list[tuple[float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


async def _run_happy_path(publisher: _FakeFindingsPublisher, findings: list[Finding]) -> None:
    step = FindingsOutputStep(publisher, depends_on="verification")
    step_input = {
        "findings": findings,
        "org_id": "org-1",
        "repo_id": "repo-1",
        "installation_id": _INSTALLATION_ID,
        "repo_full_name": _OWNER_REPO,
        "pr_number": _PR_NUMBER,
        "head_sha": _HEAD_SHA,
        "context_bundle": None,
    }
    result = await step.run(step_input, StepContext(job_id="job-1"))
    assert isinstance(result, FindingsOutputResult)


async def test_findings_count_total_recorded_by_severity_and_source(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    findings = [
        _finding(file="a.py", line=1, severity="high", source="semgrep", message="issue A"),
        _finding(file="b.py", line=2, severity="high", source="semgrep", message="issue B"),
        _finding(file="c.py", line=3, severity="low", source="llm", message="issue C"),
    ]

    await _run_happy_path(_FakeFindingsPublisher(), findings)

    points = _metric_data_points(in_memory_metric_reader, "review.findings.count_total")
    by_attrs = {tuple(sorted(attrs.items())): value for value, attrs in points}
    assert by_attrs[(("category", "none"), ("severity", "high"), ("source", "semgrep"))] == 2
    assert by_attrs[(("category", "none"), ("severity", "low"), ("source", "llm"))] == 1


async def test_findings_count_total_attributes_are_bounded_and_privacy_safe(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Per this spec's hard privacy requirement: attribute values are never
    source code/diff/finding free text — only the bounded severity/source
    vocabulary."""
    findings = [
        _finding(
            file="secret.py",
            line=1,
            severity="critical",
            source="gitleaks",
            message="AKIA-looking-secret-do-not-leak-this-string",
        ),
    ]

    await _run_happy_path(_FakeFindingsPublisher(), findings)

    points = _metric_data_points(in_memory_metric_reader, "review.findings.count_total")
    assert len(points) == 1
    _, attrs = points[0]
    assert set(attrs.keys()) == {"severity", "source", "category"}
    assert attrs == {"severity": "critical", "source": "gitleaks", "category": "none"}


async def test_dropped_by_ceiling_total_recorded_when_ceiling_exceeded(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    # 101 distinct (file, line) findings so none dedupe away, exceeding
    # findings.merge.POSTED_FINDINGS_CEILING (100) by exactly one.
    findings = [
        _finding(file=f"f{i}.py", line=i, severity="info", source="semgrep", message=f"msg {i}")
        for i in range(101)
    ]

    await _run_happy_path(_FakeFindingsPublisher(), findings)

    points = _metric_data_points(
        in_memory_metric_reader, "review.findings.dropped_by_ceiling_total"
    )
    assert points == [(1, {"category": "none"})]


async def test_dropped_by_ceiling_total_recorded_as_zero_when_not_exceeded(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    findings = [_finding(file="a.py", line=1, severity="medium", source="llm", message="only one")]

    await _run_happy_path(_FakeFindingsPublisher(), findings)

    points = _metric_data_points(
        in_memory_metric_reader, "review.findings.dropped_by_ceiling_total"
    )
    assert points == [(0, {"category": "none"})]


async def test_no_findings_records_no_count_total_but_still_records_dropped(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    await _run_happy_path(_FakeFindingsPublisher(), [])

    count_points = _metric_data_points(in_memory_metric_reader, "review.findings.count_total")
    assert count_points == []
    dropped_points = _metric_data_points(
        in_memory_metric_reader, "review.findings.dropped_by_ceiling_total"
    )
    assert dropped_points == [(0, {"category": "none"})]


async def test_citation_findings_total_counts_cited_and_uncited_findings(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/x.ts",
                symbol_name=None,
                content="x",
                line_range=(1, 2),
            ),
        )
    )

    def _llm(ids: tuple[int, ...]) -> Finding:
        return Finding(
            file="a.py",
            line=1,
            severity="medium",
            message="m",
            source="llm",
            context_ref_ids=ids,
        )

    FindingsOutputStep._apply_citations([_llm((0,)), _llm((99,)), _llm(())], bundle, job_id="job-1")

    points = _metric_data_points(in_memory_metric_reader, "review.citation.findings_total")
    # the no-ref finding is excluded so the ratio stays meaningful
    assert sorted((a["cited"], v) for v, a in points) == [("false", 1), ("true", 1)]


async def test_posted_and_ceiling_dropped_findings_are_broken_down_by_category(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Smell findings rank last at the ceiling, so the drop counter must show
    *which* category the ceiling is cutting."""
    kept = [
        _finding(file=f"k{i}.py", line=i, severity="high", source="semgrep", message=f"k{i}")
        for i in range(100)
    ]
    smells = [
        Finding(
            file=f"s{i}.py",
            line=1,
            severity="low",
            message=f"Possible Duplicated Code: {i}",
            source="llm",
            category="smell",
        )
        for i in range(3)
    ]

    await _run_happy_path(_FakeFindingsPublisher(), [*kept, *smells])

    dropped = _metric_data_points(
        in_memory_metric_reader, "review.findings.dropped_by_ceiling_total"
    )
    assert dropped == [(3, {"category": "smell"})]
    posted = {
        tuple(sorted(a.items())): v
        for v, a in _metric_data_points(in_memory_metric_reader, "review.findings.count_total")
    }
    assert posted == {(("category", "none"), ("severity", "high"), ("source", "semgrep")): 100}


async def test_v2_layout_instruments_use_closed_set_labels(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Posted-by-tier, collapsed, deduped and dropped-by-tier counters."""
    llm = {"source": "llm", "severity": "high", "line": 1}
    msg = "Missing authorization check on the delete endpoint allows any user to delete"
    findings = [
        Finding(file="a.py", message=msg, tier="blocker", **llm),
        Finding(file="b.py", message=msg, tier="blocker", **llm),
        Finding(file="c.py", message="a nit", tier="nit", **llm),
    ]
    step = FindingsOutputStep(_FakeFindingsPublisher(), depends_on="verification")
    step_input = {
        "findings": findings,
        "org_id": "org-1",
        "repo_id": "repo-1",
        "installation_id": _INSTALLATION_ID,
        "repo_full_name": _OWNER_REPO,
        "pr_number": _PR_NUMBER,
        "head_sha": _HEAD_SHA,
    }

    await step.run(step_input, StepContext(job_id="job-1", review_quality_v2=True))

    def by_attrs(name: str) -> dict[tuple[tuple[str, object], ...], float]:
        points = _metric_data_points(in_memory_metric_reader, name)
        return {tuple(sorted(attrs.items())): value for value, attrs in points}

    assert by_attrs("review.findings.posted_by_tier_total") == {(("tier", "blocker"),): 1}
    assert by_attrs("review.findings.collapsed_total") == {(("reason", "nit_hidden"),): 1}
    assert by_attrs("review.findings.deduped_total") == {(("kind", "root_cause"),): 1}
