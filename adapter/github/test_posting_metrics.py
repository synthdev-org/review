"""Metrics emitted by adapter.github.posting: post outcomes and truncation.
Reuses test_posting's respx/publisher helpers; reads the real OTel SDK via an
in-memory reader (see review/test_output_telemetry.py for the pattern)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum

from adapter.github import posting as posting_module
from adapter.github.exceptions import GitHubPostingUnreachableError
from adapter.github.test_posting import (
    _BASE,
    _HEAD_SHA,
    _INSTALLATION_ID,
    _OWNER_REPO,
    _PR_NUMBER,
    _REPO_ID,
    _finding,
    _mock_review_comments,
    _mock_reviews_post,
    _mock_token_exchange,
    _publisher,
)
from adapter.telemetry.conftest import (
    reset_otel_global_providers,  # noqa: F401
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def reader(
    reset_otel_global_providers: None,  # noqa: F811
) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _points(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, object], ...], float]:
    data = reader.get_metrics_data()
    out: dict[tuple[tuple[str, object], ...], float] = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    assert isinstance(metric.data, Sum)
                    for p in metric.data.data_points:
                        out[tuple(sorted((p.attributes or {}).items()))] = p.value
    return out


async def _post_review(key: str, **kwargs: object) -> None:
    await _publisher(key).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        findings=[_finding()],
        **kwargs,  # type: ignore[arg-type]
    )


@respx.mock
async def test_successful_review_post_counts_ok(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    _mock_token_exchange()
    _mock_reviews_post()
    _mock_review_comments()

    await _post_review(test_rsa_private_key_pem)

    points = _points(reader, "review.github.post_total")
    assert points[(("operation", "review"), ("outcome", "ok"))] == 1


@respx.mock
async def test_permanent_error_on_review_post_is_counted(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    _mock_token_exchange()
    respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(403)
    )

    with pytest.raises(GitHubPostingUnreachableError):
        await _post_review(test_rsa_private_key_pem)

    points = _points(reader, "review.github.post_total")
    assert points[(("operation", "review"), ("outcome", "permanent_error"))] == 1


@respx.mock
async def test_bare_5xx_on_review_post_counts_retry_exhausted(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    _mock_token_exchange()
    respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(GitHubPostingUnreachableError):
        await _post_review(test_rsa_private_key_pem)

    points = _points(reader, "review.github.post_total")
    assert points[(("operation", "review"), ("outcome", "retry_exhausted"))] == 1


@respx.mock
async def test_provenance_fetch_failure_is_counted_and_review_still_ok(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    _mock_token_exchange()
    _mock_reviews_post()
    _mock_review_comments()  # registers the route; override to a permanent error below
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews/99/comments").mock(
        return_value=httpx.Response(404)
    )

    await _post_review(test_rsa_private_key_pem)

    points = _points(reader, "review.github.post_total")
    assert points[(("operation", "review"), ("outcome", "ok"))] == 1
    assert points[(("operation", "provenance_fetch"), ("outcome", "permanent_error"))] == 1


@respx.mock
async def test_oversized_comment_counts_truncation(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    _mock_token_exchange()
    _mock_reviews_post()
    _mock_review_comments()
    huge = _finding(message="x" * (posting_module._COMMENT_HARD_LIMIT + 1))

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        findings=[huge],
        collapsed_findings=[],  # selects the v2 layout, the only one that truncates
    )

    points = _points(reader, "review.github.post_truncated_total")
    assert points[(("kind", "comment"),)] >= 1


@respx.mock
async def test_provenance_persist_failure_is_counted_and_review_still_ok(
    test_rsa_private_key_pem: str, reader: InMemoryMetricReader
) -> None:
    from adapter.github.test_posting import _FakeProvenanceRecorder
    from memory.store import MemoryStoreError

    class _Failing(_FakeProvenanceRecorder):
        async def record_comment_provenance(self, **kwargs: Any) -> None:
            raise MemoryStoreError(operation="record_comment_provenance", detail="db down")

    finding = _finding()
    _mock_token_exchange()
    _mock_reviews_post()
    _mock_review_comments(
        [
            {
                "id": 1001,
                "path": "app.py",
                "line": 10,
                "body": posting_module._finding_body(finding),
            }
        ]
    )

    await _publisher(test_rsa_private_key_pem, provenance_recorder=_Failing()).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        findings=[finding],
    )

    points = _points(reader, "review.github.post_total")
    assert points[(("operation", "review"), ("outcome", "ok"))] == 1
    assert points[(("operation", "provenance_persist"), ("outcome", "other"))] == 1


@pytest.mark.asyncio
async def test_review_body_over_the_hard_limit_is_truncated_and_counted(
    reader: InMemoryMetricReader,
) -> None:
    # v1 (collapsed=None) has no per-section budget, so enough file-level findings
    # genuinely exceed GitHub's body limit.
    file_level = [_finding(file=f"f{i}.py", line=None, message="m" * 300) for i in range(250)]

    body = posting_module._review_body(file_level, len(file_level))

    assert len(body) <= posting_module._BODY_HARD_LIMIT + 200
    assert "_(truncated)_" in body
    points = _points(reader, "review.github.post_truncated_total")
    assert points[(("kind", "body_hard"),)] == 1
