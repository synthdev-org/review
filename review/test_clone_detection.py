"""Unit tests for ``review.clone_detection.CloneDetectionStep`` against a fake
``CloneIndexQuery`` (the consumer Protocol boundary) and a fake ``DiffFetcher``
— real Tree-sitter parsing of the committed clone fixtures, no database
(plan-phase-10 Phase 4). The real-Postgres story tests live in
``review/test_clone_detection_step.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from findings.models import Finding
from indexing.chunk import ChunkLanguage, language_for_path, parse_file
from indexing.clone import CloneFileIndex, CloneIndexError, build_clone_index
from ingestion.diff import (
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFile,
)
from review.clone_detection import (
    CANDIDATE_FILE_CAP,
    MAX_FILES_CONSIDERED,
    PER_HASH_CANDIDATE_LIMIT,
    CloneCandidateHit,
    CloneCandidates,
    CloneDetectionOutput,
    CloneDetectionStep,
    CloneQueryError,
)
from review.context_builder import _ContextBuilderInput
from review.step import StepContext

_FIXTURES = Path(__file__).resolve().parent.parent / "indexing" / "testdata" / "clone"
_IDENTITY: dict[str, Any] = {
    "org_id": "org-1",
    "repo_id": "repo-1",
    "installation_id": 7,
    "repo_full_name": "acme/widgets",
    "pr_number": 9,
    "head_sha": "h" * 40,
    "base_sha": "b" * 40,
    "pr_title": "Add copy",
}


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _index(path: str, content: bytes) -> CloneFileIndex:
    language = language_for_path(path)
    assert language is not None
    return build_clone_index(language, path, parse_file(language, path, content))[0]


def _added_patch(content: bytes) -> str:
    lines = content.decode().splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


def _file(path: str, content: bytes | None = None, *, patch: str | None = "") -> DiffFile:
    if patch == "" and content is not None:
        patch = _added_patch(content)
    return DiffFile(path=path, status="added", patch=patch, mode="100644", blob_sha=f"sha-{path}")


class _FakeDiffFetcher:
    def __init__(self, contents: dict[str, bytes], *, error: Exception | None = None) -> None:
        self._contents = contents
        self._error = error
        self.requested: list[str] = []

    async def fetch(self, **kwargs: Any) -> Diff:
        raise AssertionError("fetch is not used by the clone step")

    async def fetch_content(
        self, *, installation_id: int, repo_full_name: str, diff: Diff, paths: Sequence[str]
    ) -> dict[str, bytes]:
        self.requested = list(paths)
        if self._error is not None:
            raise self._error
        return {path: self._contents[path] for path in paths if path in self._contents}


class _FakeCloneQuery:
    """In-memory ``CloneIndexQuery`` over stored ``CloneFileIndex``es."""

    def __init__(
        self,
        stored: dict[str, CloneFileIndex] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.stored = stored or {}
        self.error = error
        self.delay = delay
        self.excluded: list[str] | None = None
        self.loaded: list[str] = []
        self.find_calls = 0
        self.timeouts: list[float] = []

    async def find_candidates(
        self,
        *,
        repo_id: str,
        hashes: Sequence[int],
        excluded_paths: Sequence[str],
        per_hash_limit: int,
        timeout_seconds: float,
    ) -> CloneCandidates:
        self.find_calls += 1
        self.excluded = list(excluded_paths)
        self.timeouts.append(timeout_seconds)
        assert per_hash_limit == PER_HASH_CANDIDATE_LIMIT
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        wanted = set(hashes)
        hits = tuple(
            CloneCandidateHit(hash=fp.hash, file_path=path, token_offset=fp.token_offset)
            for path, index in sorted(self.stored.items())
            if path not in excluded_paths
            for fp in index.fingerprints
            if fp.hash in wanted
        )
        return CloneCandidates(hits=hits)

    async def load_files(
        self, *, repo_id: str, file_paths: Sequence[str], timeout_seconds: float
    ) -> dict[str, CloneFileIndex]:
        self.loaded = list(file_paths)
        return {path: self.stored[path] for path in file_paths if path in self.stored}


def _step(
    fetcher: _FakeDiffFetcher,
    query: _FakeCloneQuery,
    *,
    max_findings: int = 5,
    internal_timeout_seconds: float = 20.0,
) -> CloneDetectionStep:
    return CloneDetectionStep(
        fetcher,
        query,
        internal_timeout_seconds=internal_timeout_seconds,
        min_tokens=100,
        min_lines=10,
        max_findings=max_findings,
    )


async def _run(
    step: CloneDetectionStep, files: Sequence[DiffFile], incoming: Sequence[Finding] = ()
) -> CloneDetectionOutput:
    return await step.run(
        {**_IDENTITY, "diff": Diff(files=tuple(files)), "findings": list(incoming)},
        StepContext(job_id="job-1"),
    )


def _log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    (record,) = [r for r in caplog.records if r.name == "review.clone_detection"]
    return record


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="review.clone_detection")


@pytest.fixture
def in_memory_metric_reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _counter(
    reader: InMemoryMetricReader, name: str, label: str = "outcome"
) -> dict[str | None, float]:
    """A counter's data points keyed by their ``label`` attribute (None if absent)."""
    data = reader.get_metrics_data()
    assert data is not None
    return {
        (point.attributes or {}).get(label): point.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    }


_LANGUAGES = ["ts", "tsx", "php", "go"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_renamed_copy_of_an_untouched_file_is_flagged_without_quoting_code(
    ext: str, caplog: pytest.LogCaptureFixture
) -> None:
    stored_path, pr_path = f"src/billing/invoice.{ext}", f"src/billing/invoice_copy.{ext}"
    copy = _fixture(f"renamed.{ext}")
    query = _FakeCloneQuery({stored_path: _index(stored_path, _fixture(f"original.{ext}"))})

    output = await _run(_step(_FakeDiffFetcher({pr_path: copy}), query), [_file(pr_path, copy)])

    (finding,) = output.findings
    assert (finding.file, finding.line, finding.source, finding.rule_id, finding.severity) == (
        pr_path,
        1,
        "clone_detection",
        "duplication",
        "low",
    )
    assert f"`{stored_path}`" in finding.message
    assert "return" not in finding.message  # no source text is quoted
    assert _log(caplog).outcome == "completed"  # type: ignore[attr-defined]
    assert query.timeouts == [20.0]


@pytest.mark.asyncio
async def test_incoming_findings_and_job_identity_are_forwarded() -> None:
    incoming = Finding(file="a.ts", line=1, severity="high", message="m", source="semgrep")

    output = await _run(
        _step(_FakeDiffFetcher({}), _FakeCloneQuery()), [_file("a.ts", b"x\n")], [incoming]
    )

    assert output.findings == [incoming]
    assert (output.base_sha, output.pr_title, output.pr_number) == ("b" * 40, "Add copy", 9)
    # The Context Builder's input model accepts this step's output unchanged.
    accepted = _ContextBuilderInput.model_validate(output, from_attributes=True)
    assert accepted.findings == [incoming] and accepted.diff == output.diff


@pytest.mark.asyncio
async def test_empty_index_is_the_no_matches_outcome(caplog: pytest.LogCaptureFixture) -> None:
    copy = _fixture("renamed.ts")

    output = await _run(
        _step(_FakeDiffFetcher({"a.ts": copy}), _FakeCloneQuery()), [_file("a.ts", copy)]
    )

    assert output.findings == []
    assert _log(caplog).outcome == "no_matches"  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["src/a.rb", "src/a.test.ts", "tests/b.go", "src/__tests__/c.ts", "vendor/x/y.go"]
)
async def test_unsupported_test_and_vendored_files_are_never_considered(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    fetcher, query = _FakeDiffFetcher({}), _FakeCloneQuery()

    await _run(_step(fetcher, query), [_file(path, b"x\n")])

    assert _log(caplog).outcome == "no_eligible_files"  # type: ignore[attr-defined]
    assert fetcher.requested == [] and query.find_calls == 0


@pytest.mark.asyncio
async def test_file_without_a_patch_or_removed_is_not_considered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    no_patch = DiffFile(path="a.ts", status="modified", patch=None)
    removed = DiffFile(path="b.ts", status="removed", patch="@@ -1 +0,0 @@\n-x\n")

    await _run(_step(_FakeDiffFetcher({}), _FakeCloneQuery()), [no_patch, removed])

    assert _log(caplog).outcome == "no_eligible_files"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_exclusion_list_covers_modified_deleted_renamed_and_previous_paths() -> None:
    copy = _fixture("renamed.ts")
    files = [
        _file("src/new.ts", copy),
        DiffFile(path="src/mod.ts", status="modified", patch="@@ -1 +1 @@\n-a\n+b\n"),
        DiffFile(path="src/gone.ts", status="removed", patch=None),
        DiffFile(
            path="src/moved.ts",
            status="renamed",
            patch="@@ -1 +1 @@\n-a\n+b\n",
            previous_path="src/was.ts",
        ),
    ]
    query = _FakeCloneQuery()

    await _run(_step(_FakeDiffFetcher({"src/new.ts": copy}), query), files)

    assert query.excluded == [
        "src/gone.ts",
        "src/mod.ts",
        "src/moved.ts",
        "src/new.ts",
        "src/was.ts",
    ]


@pytest.mark.asyncio
async def test_stale_default_branch_copy_of_a_rewritten_file_is_ignored() -> None:
    # The PR rewrites src/invoice.ts; the index still holds its old content.
    content = _fixture("original.ts")
    query = _FakeCloneQuery({"src/invoice.ts": _index("src/invoice.ts", content)})

    output = await _run(
        _step(_FakeDiffFetcher({"src/invoice.ts": content}), query),
        [DiffFile(path="src/invoice.ts", status="modified", patch=_added_patch(content))],
    )

    assert output.findings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_pr_internal_duplicate_is_reported_once_in_the_later_sorted_path(ext: str) -> None:
    a, b = f"src/a.{ext}", f"src/b.{ext}"
    contents = {a: _fixture(f"original.{ext}"), b: _fixture(f"renamed.{ext}")}

    output = await _run(
        _step(_FakeDiffFetcher(contents), _FakeCloneQuery()),
        [_file(a, contents[a]), _file(b, contents[b])],
    )

    (finding,) = output.findings
    assert finding.file == b and f"`{a}`" in finding.message


@pytest.mark.asyncio
async def test_edit_inside_a_pre_existing_clone_is_not_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = _fixture("renamed.ts")
    query = _FakeCloneQuery({"src/old.ts": _index("src/old.ts", _fixture("original.ts"))})
    one_line = DiffFile(
        path="src/pr.ts",
        status="modified",
        patch="@@ -20,1 +20,1 @@\n-a\n+b\n",
        mode="100644",
        blob_sha="s",
    )

    output = await _run(_step(_FakeDiffFetcher({"src/pr.ts": content}), query), [one_line])

    assert output.findings == []
    assert _log(caplog).filtered_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_import_only_run_is_not_flagged(ext: str, caplog: pytest.LogCaptureFixture) -> None:
    content = _fixture(f"imports.{ext}")
    query = _FakeCloneQuery({f"src/x.{ext}": _index(f"src/x.{ext}", content)})

    output = await _run(
        _step(_FakeDiffFetcher({f"src/y.{ext}": content}), query),
        [_file(f"src/y.{ext}", content)],
    )

    assert output.findings == []
    assert _log(caplog).filtered_count == 1  # type: ignore[attr-defined]  # found, then dropped


@pytest.mark.asyncio
async def test_run_below_the_minimum_size_is_not_flagged() -> None:
    tiny = b"export const a = 1;\n" * 3
    query = _FakeCloneQuery({"src/x.ts": _index("src/x.ts", tiny)})

    output = await _run(
        _step(_FakeDiffFetcher({"src/y.ts": tiny}), query), [_file("src/y.ts", tiny)]
    )

    assert output.findings == []
    assert query.find_calls == 0  # nothing long enough to fingerprint


@pytest.mark.asyncio
async def test_colliding_fingerprints_with_different_tokens_yield_nothing() -> None:
    copy = _fixture("renamed.ts")
    real = _index("src/old.ts", _fixture("original.ts"))
    forged = real.model_copy(update={"token_hashes": tuple(h ^ 0x5A5A for h in real.token_hashes)})

    output = await _run(
        _step(_FakeDiffFetcher({"src/pr.ts": copy}), _FakeCloneQuery({"src/old.ts": forged})),
        [_file("src/pr.ts", copy)],
    )

    assert output.findings == []


@pytest.mark.asyncio
async def test_finding_cap_keeps_the_largest_and_counts_the_rest(
    caplog: pytest.LogCaptureFixture, in_memory_metric_reader: InMemoryMetricReader
) -> None:
    copy = _fixture("renamed.ts")
    paths = ["src/a.ts", "src/b.ts", "src/c.ts"]
    query = _FakeCloneQuery({"src/old.ts": _index("src/old.ts", _fixture("original.ts"))})

    output = await _run(
        _step(_FakeDiffFetcher(dict.fromkeys(paths, copy)), query, max_findings=2),
        [_file(path, copy) for path in paths],
    )

    assert len(output.findings) == 2
    record = _log(caplog)
    assert record.dropped_by_cap_count == 1  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING
    assert _counter(in_memory_metric_reader, "review.clone_detection.cap_dropped_total") == {
        None: 1
    }


@pytest.mark.asyncio
async def test_files_considered_cap_skips_and_counts_the_excess(
    caplog: pytest.LogCaptureFixture, in_memory_metric_reader: InMemoryMetricReader
) -> None:
    paths = [f"src/f{i:03}.ts" for i in range(MAX_FILES_CONSIDERED + 1)]
    fetcher = _FakeDiffFetcher(dict.fromkeys(paths, b"export const a = 1;\n"))

    await _run(
        _step(fetcher, _FakeCloneQuery()), [_file(p, b"export const a = 1;\n") for p in paths]
    )

    assert len(fetcher.requested) == MAX_FILES_CONSIDERED
    record = _log(caplog)
    assert record.files_skipped == 1  # type: ignore[attr-defined]
    assert record.files_considered == MAX_FILES_CONSIDERED  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING
    assert _counter(
        in_memory_metric_reader, "review.clone_detection.candidate_cap_dropped_total", "reason"
    ) == {"files": 1}


@pytest.mark.asyncio
async def test_candidate_file_cap_loads_at_most_the_cap_and_counts_the_excess(
    caplog: pytest.LogCaptureFixture, in_memory_metric_reader: InMemoryMetricReader
) -> None:
    copy = _fixture("renamed.ts")
    some_hash = _index("src/pr.ts", copy).fingerprints[0].hash
    hits = tuple(
        CloneCandidateHit(hash=some_hash, file_path=f"lib/f{i:03}.ts", token_offset=0)
        for i in range(CANDIDATE_FILE_CAP + 1)
    )

    class _Wide(_FakeCloneQuery):
        async def find_candidates(self, **kwargs: Any) -> CloneCandidates:
            return CloneCandidates(hits=hits, capped_hash_count=2)

    query = _Wide()

    await _run(_step(_FakeDiffFetcher({"src/pr.ts": copy}), query), [_file("src/pr.ts", copy)])

    assert len(query.loaded) == CANDIDATE_FILE_CAP
    record = _log(caplog)
    assert record.candidate_cap_dropped_count == 3  # type: ignore[attr-defined]  # 1 file + 2 hashes
    assert _counter(
        in_memory_metric_reader, "review.clone_detection.candidate_cap_dropped_total", "reason"
    ) == {"candidate_files": 1, "per_hash": 2}


@pytest.mark.asyncio
async def test_unparseable_and_untokenizable_files_do_not_hide_a_valid_copy(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    copy = _fixture("renamed.ts")
    real_build = build_clone_index

    def _build(language: ChunkLanguage, path: str, tree: Any) -> Any:
        if path == "src/odd.go":
            raise CloneIndexError(file_path=path, language=language, detail="unclassified")
        return real_build(language, path, tree)

    monkeypatch.setattr("review.clone_detection.build_clone_index", _build)
    contents = {"src/bad.ts": b"\xff\xfe not utf8", "src/odd.go": b"package x\n", "src/ok.ts": copy}
    query = _FakeCloneQuery({"src/old.ts": _index("src/old.ts", _fixture("original.ts"))})

    output = await _run(
        _step(_FakeDiffFetcher(contents), query),
        [
            _file("src/bad.ts", b"x\n"),
            _file("src/odd.go", b"x\n"),
            _file("src/ok.ts", copy),
            _file("src/skip.rb", b"x\n"),
        ],
    )

    assert [f.file for f in output.findings] == ["src/ok.ts"]
    record = [r for r in caplog.records if getattr(r, "outcome", None)][-1]
    assert record.outcome == "completed" and record.files_skipped == 2  # type: ignore[attr-defined]
    assert _counter(
        in_memory_metric_reader, "review.clone_detection.files_skipped_total", "reason"
    ) == {"unparseable": 2}


@pytest.mark.asyncio
async def test_file_missing_from_fetched_content_is_counted_as_no_content(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    await _run(_step(_FakeDiffFetcher({}), _FakeCloneQuery()), [_file("src/gone.ts", b"x\n")])

    assert _counter(
        in_memory_metric_reader, "review.clone_detection.files_skipped_total", "reason"
    ) == {"no_content": 1}


def _phases(reader: InMemoryMetricReader) -> set[str | None]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        (point.attributes or {}).get("phase")
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "review.clone_detection.phase_duration_ms"
        for point in metric.data.data_points
    }


@pytest.mark.asyncio
async def test_every_phase_is_timed_on_a_completed_run(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    copy = _fixture("renamed.ts")
    query = _FakeCloneQuery({"src/old.ts": _index("src/old.ts", _fixture("original.ts"))})

    await _run(_step(_FakeDiffFetcher({"src/pr.ts": copy}), query), [_file("src/pr.ts", copy)])

    assert _phases(in_memory_metric_reader) == {
        "fetch_content",
        "prepare",
        "find_candidates",
        "load_files",
        "match",
    }


@pytest.mark.asyncio
async def test_a_timed_out_phase_is_still_recorded(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    copy = _fixture("renamed.ts")
    step = _step(
        _FakeDiffFetcher({"src/pr.ts": copy}),
        _FakeCloneQuery(delay=5.0),
        internal_timeout_seconds=0.05,
    )

    await _run(step, [_file("src/pr.ts", copy)])

    phases = _phases(in_memory_metric_reader)
    assert "find_candidates" in phases and "match" not in phases


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("query", "failed_open_query"),
        ("unavailable", "failed_open_content"),
        ("malformed", "failed_open_content"),
        ("timeout", "failed_open_timeout"),
    ],
)
async def test_failures_return_incoming_findings_unchanged_with_a_named_outcome(
    kind: str,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    copy = _fixture("renamed.ts")
    incoming = Finding(file="a.ts", line=1, severity="high", message="m", source="semgrep")
    fetcher_error: Exception | None = {
        "unavailable": DiffContentUnavailableError(repo_full_name="acme/widgets", detail="down"),
        "malformed": DiffContentMalformedError(repo_full_name="acme/widgets", detail="bad"),
    }.get(kind)
    query = _FakeCloneQuery(
        error=CloneQueryError(operation="find_candidates", detail="boom")
        if kind == "query"
        else None,
        delay=5.0 if kind == "timeout" else 0.0,
    )
    step = _step(
        _FakeDiffFetcher({"src/pr.ts": copy}, error=fetcher_error),
        query,
        internal_timeout_seconds=0.05 if kind == "timeout" else 20.0,
    )

    output = await _run(step, [_file("src/pr.ts", copy)], [incoming])

    assert output.findings == [incoming]
    record = _log(caplog)
    assert record.outcome == outcome  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING
    assert _counter(in_memory_metric_reader, "review.clone_detection.outcome_total") == {outcome: 1}


@pytest.mark.asyncio
async def test_a_bug_is_not_swallowed() -> None:
    copy = _fixture("renamed.ts")
    query = _FakeCloneQuery(error=RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        await _run(_step(_FakeDiffFetcher({"src/pr.ts": copy}), query), [_file("src/pr.ts", copy)])
