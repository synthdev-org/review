"""Tests for review.prefilter — US3, per
docs/adr/0005-pure-no-io-logic-unit-tested.md (US3 is Unit-only per the
PRD's own Testing table: pure filtering logic with no I/O boundary of its
own — `DiffFetcher` is faked, never a live GitHub call).

Also covers US7 (docs/specs/spec-observability.md's prefilter-funnel
metrics), using OTel's own in-memory metric reader per testing.md's Unit
tier — genuinely no I/O despite `review/prefilter.py` importing the OTel
API directly (see the spec's "Resolved decisions").
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from ingestion.diff import Diff, DiffFile
from review.prefilter import (
    AGGREGATE_BYTES_CEILING,
    MATERIALIZATION_FILE_CEILING,
    PrefilterStep,
)
from review.step import StepContext

_FUNNEL_METRIC_NAMES = (
    "review.prefilter.total_files_total",
    "review.prefilter.passed_pattern_filter_total",
    "review.prefilter.dropped_by_materialization_ceiling_total",
    "review.prefilter.dropped_by_aggregate_bytes_ceiling_total",
    "review.prefilter.final_file_count_total",
)

_INPUT = {
    "org_id": "org-1",
    "repo_id": "repo-1",
    "installation_id": 555,
    "repo_full_name": "acme/widgets",
    "pr_number": 1,
    "head_sha": "a" * 40,
}


class _FakeDiffFetcher:
    def __init__(self, diff: Diff) -> None:
        self._diff = diff

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        return self._diff

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        raise NotImplementedError("review/prefilter.py must never call fetch_content()")


def _file(path: str, *, size: int = 100) -> DiffFile:
    return DiffFile(
        path=path, status="modified", mode="100644", blob_sha="sha", blob_size_bytes=size
    )


def _context() -> StepContext:
    return StepContext(job_id="job-1")


async def _run(diff: Diff) -> tuple[str, ...]:
    step = PrefilterStep(_FakeDiffFetcher(diff))
    output = await step.run(dict(_INPUT), _context())
    return tuple(f.path for f in output.diff.files)


# ---------------------------------------------------------------------------
# US3's own Given/When/Then
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lockfile_is_filtered_but_source_file_is_not() -> None:
    diff = Diff(files=(_file("package-lock.json"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


# ---------------------------------------------------------------------------
# Observability — routine per-job `info` log, unconditional on every job,
# counts filtered vs. passed through, plus the fetched-diff file count and byte size.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_log_fires_at_info_on_every_job_with_byte_size(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = Diff(files=(_file("app.py", size=123), _file("lib.py", size=77)))

    with caplog.at_level("INFO", logger="review.prefilter"):
        step = PrefilterStep(_FakeDiffFetcher(diff))
        await step.run(dict(_INPUT), _context())

    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(info_records) == 1
    assert info_records[0].getMessage() == "prefilter completed"
    assert info_records[0].fetched_diff_bytes == 200  # type: ignore[attr-defined]
    assert info_records[0].total_files == 2  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Each default ignore-pattern category, as its own case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendored_path_is_filtered() -> None:
    diff = Diff(files=(_file("vendor/some_lib/main.go"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


@pytest.mark.asyncio
async def test_generated_file_is_filtered() -> None:
    diff = Diff(files=(_file("static/bundle.min.js"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


@pytest.mark.asyncio
async def test_binary_file_is_filtered() -> None:
    diff = Diff(files=(_file("assets/logo.png"), _file("app.py")))

    surviving = await _run(diff)

    assert surviving == ("app.py",)


# ---------------------------------------------------------------------------
# Materialization file-count ceiling (default 500) — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exactly_the_ceiling_count_all_survive() -> None:
    files = tuple(_file(f"f{i}.py", size=1) for i in range(MATERIALIZATION_FILE_CEILING))
    diff = Diff(files=files)

    surviving = await _run(diff)

    assert len(surviving) == MATERIALIZATION_FILE_CEILING


@pytest.mark.asyncio
async def test_one_over_the_ceiling_drops_exactly_one(caplog: pytest.LogCaptureFixture) -> None:
    files = tuple(_file(f"f{i}.py", size=1) for i in range(MATERIALIZATION_FILE_CEILING + 1))
    diff = Diff(files=files)

    with caplog.at_level("WARNING", logger="review.prefilter"):
        surviving = await _run(diff)

    assert len(surviving) == MATERIALIZATION_FILE_CEILING
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "materialization" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# Aggregate-bytes ceiling (default ~200MB) — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_total_landing_exactly_at_the_byte_ceiling_all_survive() -> None:
    diff = Diff(
        files=(
            _file("a.py", size=AGGREGATE_BYTES_CEILING - 1),
            _file("b.py", size=1),
        )
    )

    surviving = await _run(diff)

    assert surviving == ("a.py", "b.py")


@pytest.mark.asyncio
async def test_one_byte_over_the_ceiling_drops_the_crossing_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diff = Diff(
        files=(
            _file("a.py", size=AGGREGATE_BYTES_CEILING - 1),
            _file("b.py", size=2),
        )
    )

    with caplog.at_level("WARNING", logger="review.prefilter"):
        surviving = await _run(diff)

    assert surviving == ("a.py",)
    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "aggregate" in r.getMessage()
    ]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# US7 — the funnel exported as five OTel counters, recorded at the same
# point as the log line above.
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real ``MeterProvider`` backed by ``InMemoryMetricReader``
    as the *global* one — required for ``review/prefilter.py``'s
    module-level counters (created once, at import time, against whatever
    provider was global then) to pick this test's provider up, via OTel's
    proxy-instrument upgrade mechanism (see
    ``adapter/telemetry/test_heartbeat.py``'s identical fixture, the
    established pattern this repo already uses for exactly this reason).
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _funnel_metrics(reader: InMemoryMetricReader) -> dict[str, tuple[float, dict[str, object]]]:
    """Maps each of this step's own metric names to its
    ``(value, attributes)`` pair, for whichever of them were actually
    recorded."""
    found: dict[str, tuple[float, dict[str, object]]] = {}
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name not in _FUNNEL_METRIC_NAMES:
                    continue
                assert isinstance(metric.data, Sum)
                (data_point,) = metric.data.data_points
                found[metric.name] = (data_point.value, dict(data_point.attributes or {}))
    return found


@pytest.mark.asyncio
async def test_funnel_counters_reflect_the_same_counts_as_the_log_line(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    diff = Diff(
        files=(
            _file("package-lock.json"),  # dropped by pattern filter
            _file("app.py"),
            _file("lib.py"),
        )
    )

    await _run(diff)

    found = _funnel_metrics(in_memory_metric_reader)
    assert found["review.prefilter.total_files_total"][0] == 3
    assert found["review.prefilter.passed_pattern_filter_total"][0] == 2
    assert found["review.prefilter.dropped_by_materialization_ceiling_total"][0] == 0
    assert found["review.prefilter.dropped_by_aggregate_bytes_ceiling_total"][0] == 0
    assert found["review.prefilter.final_file_count_total"][0] == 2


@pytest.mark.asyncio
async def test_dropped_by_materialization_ceiling_counter_reflects_the_drop(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    files = tuple(_file(f"f{i}.py", size=1) for i in range(MATERIALIZATION_FILE_CEILING + 1))
    diff = Diff(files=files)

    await _run(diff)

    found = _funnel_metrics(in_memory_metric_reader)
    assert found["review.prefilter.dropped_by_materialization_ceiling_total"][0] == 1
    assert found["review.prefilter.final_file_count_total"][0] == MATERIALIZATION_FILE_CEILING


@pytest.mark.asyncio
async def test_dropped_by_aggregate_bytes_ceiling_counter_reflects_the_drop(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    diff = Diff(
        files=(
            _file("a.py", size=AGGREGATE_BYTES_CEILING - 1),
            _file("b.py", size=2),
        )
    )

    await _run(diff)

    found = _funnel_metrics(in_memory_metric_reader)
    assert found["review.prefilter.dropped_by_aggregate_bytes_ceiling_total"][0] == 1
    assert found["review.prefilter.final_file_count_total"][0] == 1


@pytest.mark.asyncio
async def test_no_funnel_counter_carries_any_attribute(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Privacy/Compliance + cardinality NFR: none of the five funnel
    counters is sliced by any attribute — plain per-job counts only,
    nothing that could ever be or contain source code, diff content, or
    finding free text."""
    diff = Diff(files=(_file("app.py"), _file("lib.py")))

    await _run(diff)

    found = _funnel_metrics(in_memory_metric_reader)
    assert set(found) == set(_FUNNEL_METRIC_NAMES)
    for _value, attributes in found.values():
        assert attributes == {}


@pytest.mark.asyncio
async def test_product_knowledge_capture_fields_are_forwarded_to_output() -> None:
    """
    Forward optional identity fields.

    The optional ``JobIdentity`` fields must be explicitly forwarded by this step.
    """
    step = PrefilterStep(_FakeDiffFetcher(Diff(files=(_file("app.py"),))))
    job_input = {
        **_INPUT,
        "base_sha": "base-1",
        "pr_title": "Fix PROJ-9",
        "pr_head_ref": "bugfix/PROJ-9",
        "pr_issue_keys": ["PROJ-9", "#41"],
        "pr_is_fork": True,
        "private": True,
    }

    output = await step.run(job_input, _context())

    assert output.base_sha == "base-1"
    assert output.pr_title == "Fix PROJ-9"
    assert output.pr_head_ref == "bugfix/PROJ-9"
    assert output.pr_issue_keys == ("PROJ-9", "#41")
    assert output.pr_is_fork is True
    assert output.private is True  # fail-closed field: a dropped forward kills the feature


@pytest.mark.asyncio
async def test_cassette_and_fixture_paths_survive_prefilter() -> None:
    # Stubbing happens later in the prompt render; gitleaks/Semgrep and comment
    # anchoring still need these files in the pipeline Diff.
    paths = ("testdata/cassettes/v1.json", "tests/fixtures/a.yaml", "logs/run.jsonl", "app.py")
    diff = Diff(files=tuple(_file(p) for p in paths))

    assert await _run(diff) == paths
