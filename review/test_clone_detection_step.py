"""Integration tests for ``review.clone_detection.CloneDetectionStep`` against
real Postgres (testcontainers) with real Tree-sitter parsing; only GitHub is
faked (plan-phase-10 Phase 4; US-C4..C10, US-C14). The default-branch index is
populated through the real ``IndexingRepository.upsert_file`` writer and read
back through the real ``PostgresCloneIndexQuery``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.storage.clone_query import PostgresCloneIndexQuery
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.memory_repository import PostgresMemoryRepository
from adapter.storage.models import (
    IndexCloneFingerprintRow,
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
)
from findings.models import Finding
from indexing.chunk import Chunk, language_for_path, parse_file
from indexing.clone import build_clone_index
from indexing.embedding import EmbeddingVector
from ingestion.diff import Diff, DiffFile
from memory.fingerprint import build_finding_fingerprint
from memory.store import MemoryStoreError
from review.clone_detection import CloneCandidates, CloneDetectionOutput, CloneDetectionStep
from review.memory_suppression import MemorySuppressionConfig
from review.step import StepContext

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parent.parent / "indexing" / "testdata" / "clone"
_LANGUAGES = ["ts", "tsx", "php", "go"]


@pytest.fixture(autouse=True)
def _reenable_loggers(caplog: pytest.LogCaptureFixture) -> None:
    # Alembic's env.py (via migrated_engine) disables already-created loggers.
    logging.getLogger("review.clone_detection").disabled = False
    caplog.set_level(logging.INFO, logger="review.clone_detection")


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _added_patch(content: bytes) -> str:
    lines = content.decode().splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


def _file(path: str, content: bytes, status: str = "added", **kwargs: Any) -> DiffFile:
    return DiffFile(
        path=path,
        status=status,
        patch=_added_patch(content),
        mode="100644",
        blob_sha=f"sha-{path}",
        **kwargs,
    )


class _Fetcher:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self._contents = contents

    async def fetch(self, **kwargs: Any) -> Diff:
        raise AssertionError("not used")

    async def fetch_content(
        self, *, installation_id: int, repo_full_name: str, diff: Diff, paths: Sequence[str]
    ) -> dict[str, bytes]:
        return {path: self._contents[path] for path in paths if path in self._contents}


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _index_file(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, path: str, content: bytes
) -> None:
    """Write ``content`` at ``path`` into the default-branch index (pr_number 0)."""
    language = language_for_path(path)
    assert language is not None
    clone_index, _ = build_clone_index(language, path, parse_file(language, path, content))
    text = content.decode()
    chunk = Chunk(
        file_path=path,
        language=language,
        symbol_name=None,
        byte_range=(0, len(content)),
        line_range=None,
        content=text,
        chunk_index=0,
    )
    await IndexingRepository(session_factory).upsert_file(
        repo_id=repo_id,
        pr_number=0,
        file_path=path,
        chunks=[chunk],
        embeddings=[EmbeddingVector(values=(0.0,) * 384, dimension=384)],
        nodes=[],
        edges=[],
        clone_index=clone_index,
    )


def _step(
    session_factory: async_sessionmaker[Any],
    contents: dict[str, bytes],
    *,
    max_findings: int = 5,
    internal_timeout_seconds: float = 20.0,
    query: Any = None,
    memory_suppression: MemorySuppressionConfig | None = None,
) -> CloneDetectionStep:
    return CloneDetectionStep(
        _Fetcher(contents),
        query or PostgresCloneIndexQuery(session_factory),
        internal_timeout_seconds=internal_timeout_seconds,
        min_tokens=100,
        min_lines=10,
        max_findings=max_findings,
        memory_suppression=memory_suppression,
    )


async def _run(
    step: CloneDetectionStep,
    repo_id: uuid.UUID,
    files: Sequence[DiffFile],
    incoming: Sequence[Finding] = (),
) -> CloneDetectionOutput:
    logging.getLogger("review.clone_detection").disabled = False  # alembic env.py disables it
    return await step.run(
        {
            "org_id": "org-1",
            "repo_id": str(repo_id),
            "installation_id": 1,
            "repo_full_name": "acme/widgets",
            "pr_number": 5,
            "head_sha": "h" * 40,
            "diff": Diff(files=tuple(files)),
            "findings": list(incoming),
        },
        StepContext(job_id="job-1"),
    )


def _outcome(caplog: pytest.LogCaptureFixture) -> str:
    (record,) = [r for r in caplog.records if r.name == "review.clone_detection"]
    return str(record.outcome)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_us_c4_c7_renamed_copy_is_flagged_end_to_end_with_both_locations(
    ext: str,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored, pr_path = f"src/billing/invoice.{ext}", f"src/billing/invoice_copy.{ext}"
    await _index_file(session_factory, repo_id, stored, _fixture(f"original.{ext}"))
    copy = _fixture(f"renamed.{ext}")

    output = await _run(_step(session_factory, {pr_path: copy}), repo_id, [_file(pr_path, copy)])

    (finding,) = output.findings
    assert finding.file == pr_path and finding.line == 1
    assert (finding.source, finding.rule_id, finding.severity) == (
        "clone_detection",
        "duplication",
        "low",
    )
    assert f"`{stored}`" in finding.message and "lines 1\u2013" in finding.message
    assert "return" not in finding.message
    assert _outcome(caplog) == "completed"


@pytest.mark.asyncio
async def test_us_c4_multiple_matches_report_the_best_plus_n_more(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    for path in ("src/b.ts", "src/a.ts", "src/c.ts"):
        await _index_file(session_factory, repo_id, path, _fixture("original.ts"))
    copy = _fixture("renamed.ts")

    output = await _run(
        _step(session_factory, {"src/pr.ts": copy}), repo_id, [_file("src/pr.ts", copy)]
    )

    (finding,) = output.findings
    assert "`src/a.ts`" in finding.message and "and 2 more" in finding.message


@pytest.mark.asyncio
async def test_us_c5_edit_inside_a_pre_existing_clone_is_not_flagged(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")  # an existing clone of old.ts, one line edited below
    edit = DiffFile(
        path="src/pr.ts",
        status="modified",
        patch="@@ -20,1 +20,1 @@\n-a\n+b\n",
        mode="100644",
        blob_sha="s",
    )

    output = await _run(_step(session_factory, {"src/pr.ts": copy}), repo_id, [edit])

    assert output.findings == []
    record = next(r for r in caplog.records if r.name == "review.clone_detection")
    assert record.filtered_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_us_c5_file_without_a_patch_is_not_considered(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    no_patch = DiffFile(path="src/pr.ts", status="added", patch=None, mode="100644", blob_sha="s")

    output = await _run(
        _step(session_factory, {"src/pr.ts": _fixture("renamed.ts")}), repo_id, [no_patch]
    )

    assert output.findings == [] and _outcome(caplog) == "no_eligible_files"


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_us_c6_two_pr_files_duplicating_each_other_yield_one_finding(
    ext: str, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    contents = {
        f"src/a.{ext}": _fixture(f"original.{ext}"),
        f"src/b.{ext}": _fixture(f"renamed.{ext}"),
    }

    output = await _run(
        _step(session_factory, contents), repo_id, [_file(p, c) for p, c in contents.items()]
    )

    (finding,) = output.findings
    assert finding.file == f"src/b.{ext}" and f"`src/a.{ext}`" in finding.message


@pytest.mark.asyncio
async def test_us_c6_stale_rows_of_modified_deleted_and_renamed_files_are_ignored(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    original = _fixture("original.ts")
    for path in ("src/modified.ts", "src/deleted.ts", "src/old_name.ts"):
        await _index_file(session_factory, repo_id, path, original)
    copy = _fixture("renamed.ts")
    files = [
        _file("src/modified.ts", copy, "modified"),
        DiffFile(path="src/deleted.ts", status="removed", patch=None),
        _file("src/new_name.ts", copy, "renamed", previous_path="src/old_name.ts"),
    ]

    output = await _run(
        _step(session_factory, dict.fromkeys(("src/modified.ts", "src/new_name.ts"), copy)),
        repo_id,
        files,
    )

    # The three stale copies are excluded; the two PR files do match each
    # other, which is the one (PR-internal) finding left.
    assert [f.file for f in output.findings] == ["src/new_name.ts"]
    assert "`src/modified.ts`" in output.findings[0].message


@pytest.mark.asyncio
async def test_us_c6_a_renamed_file_without_previous_path_would_match_its_old_rows(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _index_file(session_factory, repo_id, "src/old_name.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")

    with_previous = await _run(
        _step(session_factory, {"src/new_name.ts": copy}),
        repo_id,
        [_file("src/new_name.ts", copy, "renamed", previous_path="src/old_name.ts")],
    )
    without = await _run(
        _step(session_factory, {"src/new_name.ts": copy}), repo_id, [_file("src/new_name.ts", copy)]
    )

    assert with_previous.findings == [] and len(without.findings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_us_c8_clean_code_fixture_noise_bound_is_zero_findings(
    ext: str, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _index_file(session_factory, repo_id, f"src/original.{ext}", _fixture(f"original.{ext}"))
    other = _fixture(f"coverage.{ext}")

    output = await _run(
        _step(session_factory, {f"src/other.{ext}": other}),
        repo_id,
        [_file(f"src/other.{ext}", other)],
    )

    assert output.findings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _LANGUAGES)
async def test_us_c8_import_only_run_and_test_files_are_not_flagged(
    ext: str,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    imports = _fixture(f"imports.{ext}")
    await _index_file(session_factory, repo_id, f"src/x.{ext}", imports)
    original = _fixture(f"original.{ext}")
    await _index_file(session_factory, repo_id, f"src/o.{ext}", original)
    copy = _fixture(f"renamed.{ext}")
    contents = {f"src/y.{ext}": imports, f"tests/copy.{ext}": copy}

    output = await _run(
        _step(session_factory, contents), repo_id, [_file(p, c) for p, c in contents.items()]
    )

    assert output.findings == []
    record = next(r for r in caplog.records if r.name == "review.clone_detection")
    assert record.filtered_count == 1  # type: ignore[attr-defined]  # found, then dropped
    assert output.findings == []


@pytest.mark.asyncio
async def test_us_c8_run_below_the_minimum_size_is_not_flagged(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    tiny = b"export const a = 1;\n" * 3
    await _index_file(session_factory, repo_id, "src/x.ts", tiny)

    output = await _run(
        _step(session_factory, {"src/y.ts": tiny}), repo_id, [_file("src/y.ts", tiny)]
    )

    assert output.findings == []


@pytest.mark.asyncio
async def test_us_c9_a_colliding_stored_fingerprint_with_different_tokens_yields_nothing(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    # The stored file has different code, but its fingerprint rows are rewritten
    # to carry the PR copy's hashes: every hash "collides", no token run matches.
    copy = _fixture("renamed.ts")
    await _index_file(session_factory, repo_id, "src/other.ts", _fixture("coverage.ts"))
    language = language_for_path("src/pr.ts")
    assert language is not None
    pr_index, _ = build_clone_index(language, "src/pr.ts", parse_file(language, "src/pr.ts", copy))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(IndexCloneFingerprintRow)
            .where(IndexCloneFingerprintRow.repo_id == repo_id)
            .values(hash=pr_index.fingerprints[0].hash)
        )

    output = await _run(
        _step(session_factory, {"src/pr.ts": copy}), repo_id, [_file("src/pr.ts", copy)]
    )

    assert output.findings == []


@pytest.mark.asyncio
async def test_us_c10_finding_cap_keeps_the_largest_and_logs_the_dropped_count(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")
    paths = ["src/a.ts", "src/b.ts", "src/c.ts"]

    output = await _run(
        _step(session_factory, dict.fromkeys(paths, copy), max_findings=2),
        repo_id,
        [_file(path, copy) for path in paths],
    )

    assert len(output.findings) == 2
    record = next(r for r in caplog.records if r.name == "review.clone_detection")
    assert record.dropped_by_cap_count == 1 and record.levelno == logging.WARNING  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_us_c10_adjacent_duplicated_runs_in_one_file_merge_into_one_finding(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")
    doubled = copy + copy  # two back-to-back copies of the stored code

    output = await _run(
        _step(session_factory, {"src/pr.ts": doubled}), repo_id, [_file("src/pr.ts", doubled)]
    )

    (finding,) = output.findings
    assert finding.file == "src/pr.ts" and finding.line == 1


@pytest.mark.asyncio
async def test_per_hash_candidate_cap_fires_is_logged_and_counted(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    for i in range(25):  # more stored copies than the 20-per-hash limit
        await _index_file(session_factory, repo_id, f"lib/copy{i:02}.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")

    output = await _run(
        _step(session_factory, {"src/pr.ts": copy}), repo_id, [_file("src/pr.ts", copy)]
    )

    assert len(output.findings) == 1  # still one finding, never one per location
    record = next(r for r in caplog.records if r.name == "review.clone_detection")
    assert record.candidate_cap_dropped_count > 0  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_us_c14_empty_index_returns_incoming_findings_with_no_matches(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    copy = _fixture("renamed.ts")
    incoming = Finding(file="a.ts", line=1, severity="high", message="m", source="semgrep")

    output = await _run(
        _step(session_factory, {"src/pr.ts": copy}), repo_id, [_file("src/pr.ts", copy)], [incoming]
    )

    assert output.findings == [incoming] and _outcome(caplog) == "no_matches"


@pytest.mark.asyncio
async def test_us_c14_database_error_fails_open(
    repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    dead = create_storage_engine("postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none")
    try:
        copy = _fixture("renamed.ts")
        incoming = Finding(file="a.ts", line=1, severity="high", message="m", source="semgrep")
        step = _step(
            create_session_factory(dead),
            {"src/pr.ts": copy},
            query=PostgresCloneIndexQuery(create_session_factory(dead)),
        )

        output = await _run(step, repo_id, [_file("src/pr.ts", copy)], [incoming])
    finally:
        await dead.dispose()

    assert output.findings == [incoming]
    assert _outcome(caplog) == "failed_open_query"


@pytest.mark.asyncio
async def test_us_c14_timeout_fails_open(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    class _Slow:
        async def find_candidates(self, **kwargs: Any) -> CloneCandidates:
            await asyncio.sleep(5)
            return CloneCandidates()

        async def load_files(self, **kwargs: Any) -> dict[str, Any]:
            return {}

    copy = _fixture("renamed.ts")
    step = _step(session_factory, {"src/pr.ts": copy}, internal_timeout_seconds=0.2, query=_Slow())

    output = await _run(step, repo_id, [_file("src/pr.ts", copy)])

    assert output.findings == [] and _outcome(caplog) == "failed_open_timeout"


@pytest.mark.asyncio
async def test_per_file_isolation_unparseable_and_unsupported_files_do_not_hide_a_copy(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")
    contents = {"src/bad.ts": b"\xff\xfe\x00 not utf-8", "src/ok.ts": copy, "src/x.rb": b"puts 1\n"}

    output = await _run(
        _step(session_factory, contents),
        repo_id,
        [_file("src/bad.ts", b"x\n"), _file("src/ok.ts", copy), _file("src/x.rb", b"x\n")],
    )

    assert [f.file for f in output.findings] == ["src/ok.ts"]


@pytest.fixture
def in_memory_metric_reader(reset_otel_global_providers: None) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-test"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


@pytest.mark.asyncio
async def test_outcome_and_finding_instruments_are_recorded(
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    await _index_file(session_factory, repo_id, "src/old.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")

    await _run(_step(session_factory, {"src/pr.ts": copy}), repo_id, [_file("src/pr.ts", copy)])

    data = in_memory_metric_reader.get_metrics_data()
    assert data is not None
    points = {
        m.name: list(m.data.data_points)
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    (outcome,) = points["review.clone_detection.outcome_total"]
    assert dict(outcome.attributes or {}) == {"outcome": "completed"} and outcome.value == 1
    assert points["review.clone_detection.findings_total"][0].value == 1
    assert points["review.clone_detection.candidates_total"][0].value == 1
    assert "review.clone_detection.duration_ms" in points


@pytest.mark.asyncio
async def test_us_c8_run_below_the_minimum_span_is_not_flagged(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    # Plenty of tokens (>= min_tokens) but only 9 lines (< min_lines=10).
    wide = "".join(f"export const a{i} = [{', '.join(map(str, range(30)))}];\n" for i in range(9))
    content = wide.encode()
    await _index_file(session_factory, repo_id, "src/x.ts", content)

    output = await _run(
        _step(session_factory, {"src/y.ts": content}), repo_id, [_file("src/y.ts", content)]
    )

    assert output.findings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["src/api.generated.ts", "vendor/lib/copy.ts"])
async def test_us_c8_generated_and_vendored_paths_are_not_flagged(
    path: str, session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    await _index_file(session_factory, repo_id, "src/o.ts", _fixture("original.ts"))
    copy = _fixture("renamed.ts")

    output = await _run(_step(session_factory, {path: copy}), repo_id, [_file(path, copy)])

    assert output.findings == []


# ---------------------------------------------------------------------------
# Phase 5 — Memory suppression (US-C12)
# ---------------------------------------------------------------------------

_REAL_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"
_embedding_client = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)
_comment_ids = iter(range(880_000, 890_000))


async def _new_repo(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="other", slug=f"other-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="gadgets", full_name=f"acme/gadgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _seed_dismissal(
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    finding: Finding,
    *,
    status: MemoryEntryStatus,
) -> None:
    """A ``dismissed_finding`` entry embedded from ``finding``'s fingerprint, the way the
    admin ``approve`` route embeds it (same ``build_finding_fingerprint`` composition)."""
    text = build_finding_fingerprint(
        file=finding.file, rule_id=finding.rule_id, message=finding.message
    )
    (vector,) = _embedding_client.embed([text])
    comment_id = next(_comment_ids)
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=comment_id,
                repo_id=repo_id,
                pr_number=5,
                commit_sha="b" * 40,
                finding_file=finding.file,
                finding_line=finding.line or 1,
                finding_rule_id=finding.rule_id,
                finding_message=finding.message,
            )
        )
        await session.flush()
        session.add(
            MemoryEntryRow(
                repo_id=repo_id,
                entry_type="dismissed_finding",
                status=status,
                raw_signal_type="reaction",
                raw_signal_detail={"emoji": "+1"},
                posted_finding_comment_id=comment_id,
                embedding=list(vector.values),
                approved_at=datetime.now(UTC) if status is MemoryEntryStatus.APPROVED else None,
            )
        )


async def _clone_finding(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID, pr_path: str
) -> tuple[Finding, dict[str, bytes], DiffFile]:
    """Index an original, then run the unsuppressed step on its copy: (finding, contents, file)."""
    stored = "src/billing/invoice.ts"
    await _index_file(session_factory, repo_id, stored, _fixture("original.ts"))
    copy = _fixture("renamed.ts")
    contents, file = {pr_path: copy}, _file(pr_path, copy)
    output = await _run(_step(session_factory, contents), repo_id, [file])
    (finding,) = output.findings
    return finding, contents, file


def _suppressing_step(
    session_factory: async_sessionmaker[Any], contents: dict[str, bytes]
) -> CloneDetectionStep:
    return _step(
        session_factory,
        contents,
        memory_suppression=MemorySuppressionConfig(
            memory_matcher=PostgresMemoryRepository(session_factory),
            embedding_client=_embedding_client,
            similarity_threshold=0.90,
            decay_half_life_days=180.0,
            enabled=True,
        ),
    )


@pytest.mark.asyncio
async def test_us_c12_approved_dismissal_suppresses_a_similar_clone_finding(
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    in_memory_metric_reader: InMemoryMetricReader,
    caplog: pytest.LogCaptureFixture,
) -> None:
    finding, contents, file = await _clone_finding(
        session_factory, repo_id, "src/billing/invoice_copy.ts"
    )
    await _seed_dismissal(session_factory, repo_id, finding, status=MemoryEntryStatus.APPROVED)
    caplog.clear()

    output = await _run(_suppressing_step(session_factory, contents), repo_id, [file])

    assert output.findings == []
    (record,) = [r for r in caplog.records if r.name == "review.clone_detection"]
    assert record.suppressed_by_memory_count == 1  # type: ignore[attr-defined]
    data = in_memory_metric_reader.get_metrics_data()
    assert data is not None
    (point,) = [
        p
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.clone_detection.memory_suppressed_total"
        for p in m.data.data_points
    ]
    assert point.value == 1


class _RaisingMatcher:
    async def best_effective_similarity(self, **kwargs: Any) -> float | None:
        raise MemoryStoreError(operation="match", detail="boom")


class _RaisingEmbedder:
    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        raise RuntimeError("onnx boom")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["matcher", "embedder"])
async def test_us_c14_memory_suppression_failure_fails_open(
    failure: str,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    finding, contents, file = await _clone_finding(
        session_factory, repo_id, "src/billing/invoice_copy.ts"
    )
    caplog.clear()
    config = MemorySuppressionConfig(
        memory_matcher=_RaisingMatcher(),
        embedding_client=_embedding_client if failure == "matcher" else _RaisingEmbedder(),
        similarity_threshold=0.90,
        decay_half_life_days=180.0,
        enabled=True,
    )

    output = await _run(
        _step(session_factory, contents, memory_suppression=config), repo_id, [file]
    )

    assert output.findings == [finding]
    records = [r for r in caplog.records if r.name == "review.clone_detection"]
    assert any(r.levelno == logging.WARNING and "suppression failed" in r.message for r in records)
    (final,) = [r for r in records if hasattr(r, "outcome")]
    assert final.outcome == "completed" and final.suppressed_by_memory_count == 0  # type: ignore[attr-defined]
    data = in_memory_metric_reader.get_metrics_data()
    assert data is not None
    failed = [
        p.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "review.clone_detection.memory_suppression_failed_total"
        for p in m.data.data_points
    ]
    assert failed == [1]


@pytest.mark.asyncio
async def test_us_c12_pending_entry_does_not_suppress(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    finding, contents, file = await _clone_finding(
        session_factory, repo_id, "src/billing/invoice_copy.ts"
    )
    await _seed_dismissal(session_factory, repo_id, finding, status=MemoryEntryStatus.PENDING)

    output = await _run(_suppressing_step(session_factory, contents), repo_id, [file])

    assert output.findings == [finding]


@pytest.mark.asyncio
async def test_us_c12_approved_entry_from_another_repo_does_not_suppress(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    finding, contents, file = await _clone_finding(
        session_factory, repo_id, "src/billing/invoice_copy.ts"
    )
    other = await _new_repo(session_factory)
    await _seed_dismissal(session_factory, other, finding, status=MemoryEntryStatus.APPROVED)

    output = await _run(_suppressing_step(session_factory, contents), repo_id, [file])

    assert output.findings == [finding]


@pytest.mark.asyncio
async def test_us_c12_shifted_line_range_still_suppresses_assumption_14(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> None:
    """The dismissal was captured when the other location sat a few lines lower; the
    unnormalized fingerprints still embed above the threshold (the evidence gate)."""
    finding, contents, file = await _clone_finding(
        session_factory, repo_id, "src/billing/invoice_copy.ts"
    )
    shifted = re.sub(
        "\\(lines (\\d+)\N{EN DASH}(\\d+)\\)",
        lambda m: f"(lines {int(m[1]) + 3}\N{EN DASH}{int(m[2]) + 3})",
        finding.message,
    )
    assert shifted != finding.message
    await _seed_dismissal(
        session_factory,
        repo_id,
        finding.model_copy(update={"message": shifted}),
        status=MemoryEntryStatus.APPROVED,
    )

    output = await _run(_suppressing_step(session_factory, contents), repo_id, [file])

    assert output.findings == []
