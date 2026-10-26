"""US-P2 (One pipeline): the same fixture diff and recorded model through both composition roots.

The server side is the real Worker path (`apps.worker.main._build_review_registry`, the thin wrapper
over `review.registry.build_review_registry`); the local side is
`apps.local_cli.main.build_registry`.
Both get the *same* step enablement (the local flag set, A3), or the test would compare different
pipelines. Both receive the same fake diff source and the same recorded model; only delivery
differs (a recording publisher stands in for GitHub posting and for the terminal).

The server side runs over a REAL, migrated Postgres (testcontainers): the memory, graph,
similarity and clone queries are the real adapters over empty tables. The local side reads a real
temporary git repository through `LocalGitDiffFetcher`. The server's diff source is a fixture
holding the patch GitHub would return for the same change (the GitHub API is the one thing that
cannot be real here). `Finding` has a single `line` (no end line): that is the range compared.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

import apps.worker.main as worker_main
from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.posting import GitHubFindingsPublisher
from adapter.github.tree import GitHubTreeFetcher
from adapter.local.conftest import feature_branch_repo as _feature_branch_repo
from adapter.local.conftest import requires_git
from adapter.local.diff import LocalGitDiffFetcher
from adapter.local.git import resolve_review_range
from adapter.storage.clone_query import PostgresCloneIndexQuery
from adapter.storage.conftest import alembic_config_for
from adapter.storage.context_query import PostgresGraphQuery, PostgresSimilarityQuery
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.memory_repository import PostgresMemoryRepository
from apps.local_cli.main import build_registry
from apps.local_cli.settings import LocalSettings
from apps.worker.settings import WorkerSettings
from apps.worker.test_main import (
    _EmptyTreeFetcher,
    _NoOpGitleaksAnalyzer,
    _NoOpSemgrepAnalyzer,
)
from findings.models import Finding
from findings.redact import NoOpTextRedactor
from indexing.embedding import EmbeddingVector
from ingestion.diff import Diff, DiffFile
from llmgateway.models import CompletionRequest, CompletionResponse
from review.config import StepTimeouts
from review.pipeline import StepRegistry
from review.step import StepContext

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

# What `feature_branch_repo` rewrites app.py to, and the patch `git diff -U3 main feature` gives.
_SOURCE = (
    b"def total(items):\n"
    b"    result = 0\n"
    b"    for item in items:\n"
    b"        result += item\n"
    b"    return result\n"
)
_PATCH = (
    "@@ -1,2 +1,5 @@\n"
    " def total(items):\n"
    "-    return sum(items)\n"
    "+    result = 0\n"
    "+    for item in items:\n"
    "+        result += item\n"
    "+    return result"
)


class _FixtureDiff:
    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        return Diff(
            files=(DiffFile(path="app.py", status="modified", patch=_PATCH, mode="100644"),)
        )

    async def fetch_content(
        self, *, installation_id: int, repo_full_name: str, diff: Diff, paths: Sequence[str]
    ) -> dict[str, bytes]:
        return {"app.py": _SOURCE}


class _ZeroEmbedder:
    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(EmbeddingVector(values=(0.0, 0.0, 0.0), dimension=3) for _ in texts)


@pytest.fixture(scope="module")
def _pgvector() -> Iterator[PostgresContainer]:
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def _engine(_pgvector: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    dsn = _pgvector.get_connection_url()
    previous = os.environ.get("REVIEW_DATABASE_URL")
    config = alembic_config_for(dsn)  # sets REVIEW_DATABASE_URL, restored below
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_storage_engine(dsn)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, config, "base")
        if previous is None:
            os.environ.pop("REVIEW_DATABASE_URL", None)
        else:
            os.environ["REVIEW_DATABASE_URL"] = previous


class _RecordedModel:
    """Answers each pipeline stage the way a real model would; identical for both roots."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        system = request.messages[0].content
        if "verification assistant" in system:
            content = json.dumps(
                {
                    "verdicts": [
                        {
                            "index": 0,
                            "valid": True,
                            "actionable": True,
                            "specific": True,
                            "valuable": True,
                        }
                    ]
                }
            )
        elif "summary (2-4 sentences)" in system:
            content = "Adds total()."
        else:
            content = json.dumps(
                {
                    "findings": [
                        {
                            "file": "app.py",
                            "line": 2,
                            "severity": "high",
                            "message": "result starts as an int, so floats mix types",
                        }
                    ]
                }
            )
        return CompletionResponse(content=content, model=request.model)


class _CollectingPublisher:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    async def post_review(self, *, findings: list[Finding], **_: object) -> None:
        self.findings.extend(findings)

    async def post_check_run(self, **_: object) -> None:
        return None

    async def post_failure_check_run(self, **_: object) -> None:
        raise AssertionError("the pipeline failed")


_SEED: Mapping[str, object] = {
    "org_id": "00000000-0000-0000-0000-000000000001",
    "repo_id": "00000000-0000-0000-0000-000000000002",
    "installation_id": 1,
    "repo_full_name": "acme/widgets",
    "pr_number": 7,
    "head_sha": "d" * 40,
}


def _shape(findings: list[Finding]) -> list[tuple[str, int | None, str, str]]:
    """File, line range, severity and message: the fields US-P2 requires to be identical."""
    return sorted((f.file, f.line, str(f.severity), f.message) for f in findings)


async def _run(registry: StepRegistry, *, head_sha: str) -> None:
    seed = {**_SEED, "head_sha": head_sha}
    outcomes = await registry.run(seed_input=seed, context=StepContext(job_id="job-1"))
    assert outcomes["output"].status == "success"
    assert outcomes["review_generation"].status == "success"


@pytest.mark.asyncio
async def test_us_p2_both_composition_roots_yield_identical_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _engine: AsyncEngine
) -> None:
    monkeypatch.setattr(worker_main, "GitleaksAnalyzer", _NoOpGitleaksAnalyzer)
    monkeypatch.setattr(worker_main, "SemgrepAnalyzer", _NoOpSemgrepAnalyzer)
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    # REVIEW_DATABASE_URL already points at the real container (set by the `_engine` fixture).
    monkeypatch.setenv("REVIEW_WORKER_GITHUB_APP_ID", "1")
    monkeypatch.setenv("REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY", "unused")
    local_settings = LocalSettings(
        llm_endpoint="http://127.0.0.1:1/v1", llm_model="m", static_analysis_enabled=False
    )
    enablement = local_settings.step_enablement()  # the same disabled steps in both roots

    repo = _feature_branch_repo(tmp_path / "repo")
    review_range = await resolve_review_range(repo.path, "main", environ=os.environ)
    local_diff = LocalGitDiffFetcher(repo.path, review_range, environ=os.environ)
    # The fixture patch must be what git really produces, or the two sides review different diffs.
    fetched = await local_diff.fetch(
        installation_id=0, repo_full_name="local/x", pr_number=0, head_sha=review_range.head_sha
    )
    assert [(f.path, f.status, f.patch) for f in fetched.files] == [("app.py", "modified", _PATCH)]

    session_factory = create_session_factory(_engine)
    server_publisher, local_publisher = _CollectingPublisher(), _CollectingPublisher()
    server = worker_main._build_review_registry(
        enablement,
        StepTimeouts(),
        cast("GitHubDiffFetcher", _FixtureDiff()),
        cast("GitHubTreeFetcher", _EmptyTreeFetcher()),
        PostgresGraphQuery(session_factory),
        PostgresSimilarityQuery(session_factory),
        PostgresCloneIndexQuery(session_factory),
        cast("LocalONNXEmbeddingClient", _ZeroEmbedder()),
        NoOpTextRedactor(),
        _RecordedModel(),
        cast("GitHubFindingsPublisher", server_publisher),
        WorkerSettings(_env_file=None),
        PostgresMemoryRepository(session_factory),
        None,
    )
    local = build_registry(
        local_settings,
        model="m",
        diff_fetcher=local_diff,
        llm_client=_RecordedModel(),
        publisher=local_publisher,
        environ=os.environ,
    )

    await _run(server, head_sha=review_range.head_sha)
    await _run(local, head_sha=review_range.head_sha)

    assert server_publisher.findings, (
        "the fixture must produce a finding or the comparison is vacuous"
    )
    assert _shape(local_publisher.findings) == _shape(server_publisher.findings)
