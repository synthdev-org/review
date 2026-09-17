"""Integration tests for the GitHub webhook receiver route, driven through
the actual apps/webhook_api FastAPI app via TestClient (no real network
socket, per docs/rules/testing.md, but exercises routing/request-parsing
beyond adapter.github.signature's pure verification function).

Per the plan's Phase 4 Tests section: "the same three failure cases plus
the happy path returning 200/401; a malformed-JSON or missing-installation-
ID payload that still carries a valid signature returns a clean 4xx rather
than an unhandled 500; an oversized body and a rate-limit-exceeding burst
are both rejected before signature verification runs."

Per the plan's Phase 1 Tests section, this is also the first phase whose
test needs a real DB *write*, not just a real DB read — tests that
actually exercise installation/org/repo resolution and enqueueing use a
real Postgres via testcontainers (`migrated_engine`/`pgvector_container`,
imported into this package's `conftest.py` from
`adapter/storage/conftest.py`); tests that only prove pre-DB rejection
paths (signature/body-size/rate-limit/malformed-payload) use an
unreachable placeholder DSN, since `create_app` never issues a query to
build those responses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapter.storage.engine import create_session_factory
from adapter.storage.models import JobQueueRow, JobStatus, JobType, Org, Repo, WebhookDeliveryRow
from adapter.storage.queue import JobQueueRepository
from apps.webhook_api.main import create_app
from apps.webhook_api.settings import WebhookAPISettings

pytestmark = pytest.mark.integration

_SECRET = "integration-test-webhook-secret"
_URL = "/webhooks/github"
_UNREACHABLE_DSN = "postgresql+asyncpg://unused:unused@localhost:5432/unused"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_EVENT_HEADER = "X-GitHub-Event"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_client(
    *,
    dsn: str = _UNREACHABLE_DSN,
    max_body_bytes: int = 1_000_000,
    rate_limit_max_requests: int = 1_000,
    rate_limit_window_seconds: float = 60.0,
    review_enqueue_rate_limit_per_minute: int = 0,
    indexing_enabled: bool = False,
    index_enqueue_rate_limit_per_minute: int = 5,
) -> TestClient:
    settings = WebhookAPISettings(
        environment="development",
        database_url=SecretStr(dsn),
        github_webhook_secret=SecretStr(_SECRET),
        webhook_max_body_bytes=max_body_bytes,
        webhook_rate_limit_max_requests=rate_limit_max_requests,
        webhook_rate_limit_window_seconds=rate_limit_window_seconds,
        review_enqueue_rate_limit_per_minute=review_enqueue_rate_limit_per_minute,
        indexing_enabled=indexing_enabled,
        index_enqueue_rate_limit_per_minute=index_enqueue_rate_limit_per_minute,
        _env_file=None,
    )
    return TestClient(create_app(settings))


def _payload(
    *,
    action: str | None = "opened",
    repo_full_name: str = "acme/widgets",
    installation_id: int = 123,
    pr_number: int | None = 7,
    head_sha: str | None = "deadbeefcafe",
    base_sha: str | None = None,
    include_pull_request: bool = True,
) -> bytes:
    body: dict[str, object] = {"installation": {"id": installation_id}}
    if action is not None:
        body["action"] = action
    body["repository"] = {"full_name": repo_full_name}
    if include_pull_request:
        pr: dict[str, object] = {}
        if pr_number is not None:
            pr["number"] = pr_number
        if head_sha is not None:
            pr["head"] = {"sha": head_sha}
        if base_sha is not None:
            pr["base"] = {"sha": base_sha}
        body["pull_request"] = pr
    return json.dumps(body).encode("utf-8")


def _push_payload(
    *,
    repo_full_name: str = "acme/widgets",
    installation_id: int = 123,
    default_branch: str = "main",
    ref: str = "refs/heads/main",
    after: str | None = "commit-after-sha",
    before: str | None = "commit-before-sha",
    commits: list[dict[str, object]] | None = None,
    head_commit: dict[str, object] | None = None,
    forced: bool | None = False,
    deleted: bool | None = None,
) -> bytes:
    body: dict[str, object] = {
        "installation": {"id": installation_id},
        "repository": {"full_name": repo_full_name, "default_branch": default_branch},
        "ref": ref,
    }
    if before is not None:
        body["before"] = before
    if after is not None:
        body["after"] = after
    if commits is not None:
        body["commits"] = commits
    if head_commit is not None:
        body["head_commit"] = head_commit
    if forced is not None:
        body["forced"] = forced
    if deleted is not None:
        body["deleted"] = deleted
    return json.dumps(body).encode("utf-8")


def _post(
    client: TestClient,
    body: bytes,
    *,
    event_type: str | None = "pull_request",
    delivery_id: str | None = None,
    secret: str = _SECRET,
) -> httpx.Response:
    headers = {"X-Hub-Signature-256": _sign(body, secret)}
    if event_type is not None:
        headers[_EVENT_HEADER] = event_type
    if delivery_id is not None:
        headers[_DELIVERY_HEADER] = delivery_id
    response: httpx.Response = client.post(_URL, content=body, headers=headers)
    return response


async def _org_repo_counts(engine: AsyncEngine) -> tuple[int, int]:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        org_count = (await session.execute(select(func.count()).select_from(Org))).scalar_one()
        repo_count = (await session.execute(select(func.count()).select_from(Repo))).scalar_one()
    return org_count, repo_count


async def _job_count(engine: AsyncEngine) -> int:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(JobQueueRow))).scalar_one()


async def _delivery_count(engine: AsyncEngine) -> int:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(WebhookDeliveryRow))
        ).scalar_one()


async def _fetch_jobs(engine: AsyncEngine) -> list[JobQueueRow]:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(select(JobQueueRow))
        return list(result.scalars().all())


# --- Pre-DB rejection paths (unreachable placeholder DSN; no query issued) ---
#
# Every response that reaches 200 runs installation/org/repo resolution
# (tenancy.resolution.resolve_installation), which is real Postgres I/O —
# per the plan's Phase 1 section, this runs "for every accepted webhook,
# regardless of trigger-set membership". Only tests whose every request
# stays rejected (401/413/422/429, never 200) can safely use the
# unreachable placeholder DSN below; the two below that were previously in
# this group (a plain 200, and a rate-limit burst whose first two requests
# succeed) moved to the real-Postgres group further down.


def test_wrong_secret_signature_returns_401() -> None:
    client = _build_client()
    body = _payload()

    response = _post(client, body, secret="a-totally-different-secret")

    assert response.status_code == 401


def test_tampered_payload_returns_401() -> None:
    client = _build_client()
    body = _payload()
    signature = _sign(body)
    tampered_body = body.replace(b"123", b"999")

    response = client.post(_URL, content=tampered_body, headers={"X-Hub-Signature-256": signature})

    assert response.status_code == 401


def test_missing_signature_header_returns_401() -> None:
    client = _build_client()

    response = client.post(_URL, content=_payload())

    assert response.status_code == 401


def test_oversized_body_returns_413_before_signature_check() -> None:
    client = _build_client(max_body_bytes=10)
    body = _payload()
    assert len(body) > 10

    # Deliberately an *invalid* signature: proves the 413 fires from the
    # body-size limit, not incidentally from signature verification having
    # run first and separately rejected it with a 401.
    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})

    assert response.status_code == 413


def test_rate_limit_fires_before_signature_verification() -> None:
    """Proves the rejection ORDER, not just that both checks individually
    work: every request below carries an invalid signature, so if signature
    verification ran first every response would be 401 regardless of the
    rate limit. Getting 429 on the third request instead proves the rate
    limiter runs — and rejects — before signature verification does, per
    the plan's Phase 4 section and the module docstring's stated ordering.
    """
    client = _build_client(rate_limit_max_requests=2)
    body = _payload()
    headers = {"X-Hub-Signature-256": "sha256=deadbeef"}

    first = client.post(_URL, content=body, headers=headers)
    second = client.post(_URL, content=body, headers=headers)
    third = client.post(_URL, content=body, headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429


def test_malformed_json_with_valid_signature_returns_clean_4xx() -> None:
    client = _build_client()
    body = b"this is not json"

    response = _post(client, body, event_type=None)

    assert 400 <= response.status_code < 500
    assert response.json()["detail"]


def test_missing_installation_id_with_valid_signature_returns_clean_4xx() -> None:
    client = _build_client()
    body = json.dumps({"action": "opened"}).encode("utf-8")

    response = _post(client, body, event_type=None)

    assert 400 <= response.status_code < 500
    assert response.json()["detail"]


def test_rejected_webhook_log_never_contains_raw_payload_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per the plan's Observability & rollback section: rejections are
    logged "never the payload body, which may carry sensitive repo/org
    data". A distinctive marker embedded in the (invalidly signed) body
    below must not leak into any log record emitted while handling it.
    """
    # adapter/storage's Alembic env.py calls logging.config.fileConfig(),
    # which (default disable_existing_loggers=True) disables every
    # already-created logger not explicitly listed in alembic.ini —
    # including this one, if a migration/storage test happened to run
    # earlier in the same pytest process (they do, alphabetically, in the
    # full integration run). Re-enabling explicitly keeps this test
    # deterministic regardless of test order, without touching Phase 2's
    # (unrelated, pre-existing) Alembic logging config.
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    client = _build_client()
    marker = "definitely-sensitive-repo-name-marker"
    body = json.dumps({"installation": {"id": 123}, "secret_repo": marker}).encode("utf-8")

    with caplog.at_level("WARNING"):
        response = client.post(
            _URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}
        )

    assert response.status_code == 401
    assert marker not in caplog.text
    assert any("webhook rejected" in record.getMessage() for record in caplog.records)


# --- US8 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG):
# `review.webhook.rejected_total`, one `.add(1, {"reason": ...})` per
# rejection reason, recorded at the exact same call site as the
# `logger.warning("webhook rejected", ...)` lines exercised above. Uses
# OTel's own in-memory metric reader (no I/O beyond the same in-process
# TestClient calls every other test in this file already makes) — see
# `review/test_output_telemetry.py` for the identical fixture pattern. ---


@pytest.fixture
def in_memory_metric_reader(
    reset_otel_global_providers: None,
) -> Iterator[InMemoryMetricReader]:
    """Registers a real `MeterProvider` backed by `InMemoryMetricReader` as
    the global one, so `webhook_routes.py`'s module-level `meter`/counter
    (created once, at import time) picks it up via OTel's proxy-instrument
    migration mechanism — see `review/test_output_telemetry.py`'s identical
    fixture docstring.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "review-webhook-api-test"}),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    yield reader
    provider.shutdown()


def _rejected_total_points(reader: InMemoryMetricReader) -> list[tuple[float, dict[str, object]]]:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    points: list[tuple[float, dict[str, object]]] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != "review.webhook.rejected_total":
                    continue
                assert isinstance(metric.data, Sum)
                for data_point in metric.data.data_points:
                    points.append((data_point.value, dict(data_point.attributes or {})))
    return points


def _rejected_count_for(reader: InMemoryMetricReader, reason: str) -> int:
    by_reason = {attrs.get("reason"): value for value, attrs in _rejected_total_points(reader)}
    return int(by_reason.get(reason, 0))


def test_rate_limited_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    client = _build_client(rate_limit_max_requests=1)
    body = _payload()
    headers = {"X-Hub-Signature-256": "sha256=deadbeef"}
    client.post(_URL, content=body, headers=headers)

    response = client.post(_URL, content=body, headers=headers)

    assert response.status_code == 429
    assert _rejected_count_for(in_memory_metric_reader, "rate_limited") == 1


def test_body_too_large_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    client = _build_client(max_body_bytes=10)
    body = _payload()
    assert len(body) > 10

    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})

    assert response.status_code == 413
    assert _rejected_count_for(in_memory_metric_reader, "body_too_large") == 1


def test_missing_signature_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    client = _build_client()

    response = client.post(_URL, content=_payload())

    assert response.status_code == 401
    assert _rejected_count_for(in_memory_metric_reader, "missing_signature") == 1


def test_invalid_signature_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    client = _build_client()
    body = _payload()

    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})

    assert response.status_code == 401
    assert _rejected_count_for(in_memory_metric_reader, "invalid_signature") == 1


def test_malformed_json_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    client = _build_client()
    body = b"this is not json"

    response = _post(client, body, event_type=None)

    assert 400 <= response.status_code < 500
    assert _rejected_count_for(in_memory_metric_reader, "malformed_json") == 1


def test_invalid_payload_shape_rejection_increments_rejected_total(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Regression test: `invalid_payload_shape` is the one rejection reason
    that was missing its `_rejected_counter.add(...)` call alongside the
    `logger.warning(...)` it already shipped with."""
    client = _build_client()
    body = json.dumps({"action": "opened"}).encode("utf-8")

    response = _post(client, body, event_type=None)

    assert 400 <= response.status_code < 500
    assert _rejected_count_for(in_memory_metric_reader, "invalid_payload_shape") == 1


def test_rejected_total_attributes_are_bounded_and_privacy_safe(
    in_memory_metric_reader: InMemoryMetricReader,
) -> None:
    """Per this spec's hard privacy requirement: attribute values are never
    payload/body content — only the closed `reason` vocabulary already
    passed to the co-located `logger.warning(...)` call."""
    client = _build_client()

    client.post(_URL, content=_payload())

    points = _rejected_total_points(in_memory_metric_reader)
    assert len(points) == 1
    _, attrs = points[0]
    assert set(attrs.keys()) == {"reason"}
    assert attrs == {"reason": "missing_signature"}


# --- Installation/Org/Repo resolution + idempotent enqueue (Phase 1, real Postgres) ---


@pytest.mark.asyncio
async def test_valid_signature_returns_200(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(action=None, include_pull_request=False)

    response = _post(client, body, event_type=None, delivery_id="delivery-plain")

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


@pytest.mark.asyncio
async def test_rate_limit_exceeding_burst_returns_429(migrated_engine: AsyncEngine) -> None:
    body = _payload(action="closed")  # a real, harmless trigger-non-matching action
    headers = {"X-Hub-Signature-256": _sign(body), _EVENT_HEADER: "pull_request"}

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        rate_limit_max_requests=2,
    ) as client:
        first = client.post(_URL, content=body, headers={**headers, _DELIVERY_HEADER: "rl-1"})
        second = client.post(_URL, content=body, headers={**headers, _DELIVERY_HEADER: "rl-2"})
        third = client.post(_URL, content=body, headers={**headers, _DELIVERY_HEADER: "rl-3"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


@pytest.mark.asyncio
async def test_unseen_installation_creates_org_repo_and_enqueues_one_job(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(repo_full_name="acme/widgets", installation_id=555, pr_number=1, head_sha="a1")

    response = _post(client, body, delivery_id="delivery-1")

    assert response.status_code == 200
    org_count, repo_count = await _org_repo_counts(migrated_engine)
    assert (org_count, repo_count) == (1, 1)
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == JobType.REVIEW
    assert job.status == JobStatus.PENDING
    assert job.payload["installation_id"] == 555
    assert job.payload["repo_full_name"] == "acme/widgets"
    assert job.payload["pr_number"] == 1
    assert job.payload["head_sha"] == "a1"
    assert job.org_id is not None
    assert job.repo_id is not None


@pytest.mark.asyncio
async def test_base_sha_is_populated_from_pull_request_base_sha(
    migrated_engine: AsyncEngine,
) -> None:
    """docs/plans/plan-deliver-context-builder.md's Phase 1 section:
    `_build_webhook_event`/`_review_enqueue_payload` must actually carry
    `pull_request.base.sha` into the enqueued job payload — without this, a
    wiring bug (wrong JSON key, dropped field) would go undetected despite
    `base_sha` being load-bearing for Phase 5's citation accuracy
    (Assumption 1/2).
    """
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(
        repo_full_name="acme/widgets",
        installation_id=556,
        pr_number=1,
        head_sha="a1",
        base_sha="base-a1",
    )

    response = _post(client, body, delivery_id="delivery-base-sha")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    assert jobs[0].payload["base_sha"] == "base-a1"


@pytest.mark.asyncio
async def test_missing_base_sha_still_enqueues_with_null_base_sha(
    migrated_engine: AsyncEngine,
) -> None:
    """`base_sha` is optional (per Assumption 2) — a delivery missing
    `pull_request.base.sha` must still enqueue a REVIEW job (unlike
    `head_sha`, whose absence blocks enqueueing entirely), just with
    `base_sha: None` in the payload.
    """
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(
        repo_full_name="acme/widgets",
        installation_id=557,
        pr_number=1,
        head_sha="a1",
        base_sha=None,
    )

    response = _post(client, body, delivery_id="delivery-no-base-sha")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    assert jobs[0].payload["base_sha"] is None


@pytest.mark.asyncio
async def test_known_installation_resolves_existing_rows_and_enqueues(
    migrated_engine: AsyncEngine,
) -> None:
    first_body = _payload(
        repo_full_name="acme/widgets", installation_id=555, pr_number=1, head_sha="a1"
    )
    second_body = _payload(
        action="synchronize",
        repo_full_name="acme/widgets",
        installation_id=555,
        pr_number=1,
        head_sha="a2",
    )

    # `with client:` reuses one anyio portal (one event loop) across both
    # calls below — without it, each `TestClient.post()` opens its own
    # ephemeral portal, and the pooled asyncpg connection this route's
    # first DB call opens under one loop cannot be reused under another.
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        first = _post(client, first_body, delivery_id="delivery-1")
        second = _post(client, second_body, delivery_id="delivery-2")

    assert first.status_code == 200
    assert second.status_code == 200
    org_count, repo_count = await _org_repo_counts(migrated_engine)
    assert (org_count, repo_count) == (1, 1)
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 2
    assert jobs[0].org_id == jobs[1].org_id
    assert jobs[0].repo_id == jobs[1].repo_id


@pytest.mark.asyncio
async def test_same_delivery_id_processed_twice_yields_exactly_one_job(
    migrated_engine: AsyncEngine,
) -> None:
    body = _payload(repo_full_name="acme/widgets", installation_id=555)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        first = _post(client, body, delivery_id="repeated-delivery")
        second = _post(client, body, delivery_id="repeated-delivery")

    assert first.status_code == 200
    assert second.status_code == 200
    assert await _job_count(migrated_engine) == 1
    assert await _delivery_count(migrated_engine) == 1


@pytest.mark.asyncio
async def test_action_outside_trigger_set_accepted_but_not_enqueued(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(action="closed", repo_full_name="acme/widgets", installation_id=555)

    response = _post(client, body, delivery_id="delivery-closed")

    assert response.status_code == 200
    assert await _job_count(migrated_engine) == 0
    # Delivery/org/repo resolution still runs unconditionally.
    assert await _delivery_count(migrated_engine) == 1


@pytest.mark.asyncio
async def test_push_event_accepted_and_not_enqueued(migrated_engine: AsyncEngine) -> None:
    """A real `push` payload has no top-level `action` key and no
    `pull_request` object — proves the envelope's optional fields don't
    reject real non-PR traffic."""
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = json.dumps(
        {"installation": {"id": 555}, "repository": {"full_name": "acme/widgets"}}
    ).encode("utf-8")

    response = _post(client, body, event_type="push", delivery_id="delivery-push")

    assert response.status_code == 200
    assert await _job_count(migrated_engine) == 0


@pytest.mark.asyncio
async def test_repository_less_event_accepted_without_touching_tenancy() -> None:
    """A real `ping`/`installation`-family payload carries no top-level
    `repository` key at all — proves the route accepts it without ever
    resolving org/repo (asserted here by using the unreachable placeholder
    DSN: if the route attempted any DB work for this payload, the request
    would fail rather than return 200)."""
    client = _build_client()
    body = json.dumps({"installation": {"id": 555}}).encode("utf-8")

    response = _post(client, body, event_type="ping", delivery_id="delivery-ping")

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


@pytest.mark.asyncio
async def test_missing_pull_request_number_accepted_not_enqueued(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(
        repo_full_name="acme/widgets", installation_id=555, pr_number=None, head_sha="a1"
    )

    with caplog.at_level("INFO"):
        response = _post(client, body, delivery_id="delivery-missing-number")

    assert response.status_code == 200
    assert await _job_count(migrated_engine) == 0
    assert any(
        getattr(record, "skipped_reason", None) == "missing_required_fields"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_missing_head_sha_accepted_not_enqueued(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(repo_full_name="acme/widgets", installation_id=555, pr_number=1, head_sha=None)

    with caplog.at_level("INFO"):
        response = _post(client, body, delivery_id="delivery-missing-sha")

    assert response.status_code == 200
    assert await _job_count(migrated_engine) == 0
    assert any(
        getattr(record, "skipped_reason", None) == "missing_required_fields"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_enqueues_past_rate_limit_are_rejected_at_the_boundary(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    def _body(n: int) -> bytes:
        return _payload(
            repo_full_name="acme/widgets", installation_id=555, pr_number=n, head_sha=f"sha{n}"
        )

    with (
        _build_client(
            dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
            review_enqueue_rate_limit_per_minute=2,
        ) as client,
        caplog.at_level("INFO"),
    ):
        first = _post(client, _body(1), delivery_id="rl-1")
        second = _post(client, _body(2), delivery_id="rl-2")
        third = _post(client, _body(3), delivery_id="rl-3")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert await _job_count(migrated_engine) == 2
    assert any(
        getattr(record, "skipped_reason", None) == "rate_limited" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_default_configuration_never_rate_limits(migrated_engine: AsyncEngine) -> None:
    """Phase 1's own shipped default (`review_enqueue_rate_limit_per_minute=0`)
    imposes no rate limiting at all — proves the disabled-by-default claim
    rather than leaving it unverified."""
    burst_size = 20

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        for i in range(burst_size):
            response = _post(
                client,
                _payload(
                    repo_full_name="acme/widgets",
                    installation_id=555,
                    pr_number=i,
                    head_sha=f"sha{i}",
                ),
                delivery_id=f"burst-{i}",
            )
            assert response.status_code == 200

    assert await _job_count(migrated_engine) == burst_size


# --- Indexing (INDEX job) enqueue path (plan-indexing-delivery.md US1/US2/US11) ---


@pytest.mark.asyncio
async def test_push_to_default_branch_enqueues_one_index_job(migrated_engine: AsyncEngine) -> None:
    """US1: a `push` to the repo's default branch, with the kill switch on,
    enqueues exactly one `INDEX` job carrying the merged file lists and the
    pushed HEAD commit sha. Before this test, `_push_payload` (defined
    above) was never called anywhere in this file, so this path was
    completely untested despite the plan's traceability table naming this
    file as US1's coverage.
    """
    client = _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    )
    body = _push_payload(
        repo_full_name="acme/widgets",
        installation_id=555,
        after="commit-after-sha",
        commits=[{"added": ["a.ts"], "removed": [], "modified": ["b.ts"]}],
    )

    response = _post(client, body, event_type="push", delivery_id="delivery-push-index")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == JobType.INDEX
    assert job.status == JobStatus.PENDING
    assert job.payload["pr_number"] == 0
    assert job.payload["commit_sha"] == "commit-after-sha"
    assert job.payload["added"] == ["a.ts"]
    assert job.payload["modified"] == ["b.ts"]


@pytest.mark.asyncio
async def test_push_with_merge_commit_still_indexes_head_commit_files(
    migrated_engine: AsyncEngine,
) -> None:
    """A push landed via a merge (e.g. GitHub's "Merge pull request" button)
    can have an empty `commits` array — GitHub's `push` payload excludes
    merge commits from it — with the actual file changes present only on
    `head_commit`. Before the fix, `_merged_push_file_lists` only ever
    read `envelope.commits`, so this case silently enqueued an `INDEX` job
    with empty `added`/`removed`/`modified` while still advancing the
    indexed-commit cursor, permanently skipping the merge's own changes.
    """
    client = _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    )
    body = _push_payload(
        repo_full_name="acme/widgets",
        installation_id=555,
        after="merge-commit-sha",
        commits=[],
        head_commit={"added": ["a.ts"], "removed": [], "modified": ["b.ts"]},
    )

    response = _post(client, body, event_type="push", delivery_id="delivery-push-merge-commit")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == JobType.INDEX
    assert job.payload["added"] == ["a.ts"]
    assert job.payload["modified"] == ["b.ts"]


@pytest.mark.asyncio
async def test_push_deleting_default_branch_enqueues_no_index_job(
    migrated_engine: AsyncEngine,
) -> None:
    """A branch/tag-deletion push carries `after` as the all-zeros SHA (a
    string, not absent) and GitHub's own `deleted: true` flag — `after is
    None` alone never catches this, so without the `deleted` check this
    would enqueue an `INDEX` job for a commit that never existed.
    """
    client = _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    )
    body = _push_payload(
        repo_full_name="acme/widgets",
        installation_id=555,
        after="0" * 40,
        deleted=True,
    )

    response = _post(client, body, event_type="push", delivery_id="delivery-push-deleted")

    assert response.status_code == 200
    assert await _fetch_jobs(migrated_engine) == []


@pytest.mark.asyncio
async def test_pull_request_opened_enqueues_review_and_index_jobs(
    migrated_engine: AsyncEngine,
) -> None:
    """US2: a `pull_request opened` event, with the kill switch on,
    enqueues both the pre-existing `REVIEW` job and a new `INDEX` job from
    the same request/shared transaction. Before this test, no test in this
    file ever passed `indexing_enabled=True`, so `_index_enqueue_payload`'s
    `pull_request` branch never executed in any test.
    """
    client = _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    )
    body = _payload(
        repo_full_name="acme/widgets", installation_id=555, pr_number=9, head_sha="pr-sha-9"
    )

    response = _post(client, body, delivery_id="delivery-pr-index")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 2
    jobs_by_type = {job.job_type: job for job in jobs}
    assert set(jobs_by_type) == {JobType.REVIEW, JobType.INDEX}
    index_job = jobs_by_type[JobType.INDEX]
    assert index_job.payload["pr_number"] == 9
    assert index_job.payload["commit_sha"] == "pr-sha-9"
    assert index_job.payload["force_full_tree_fallback"] is True
    review_job = jobs_by_type[JobType.REVIEW]
    assert review_job.payload["pr_number"] == 9
    assert review_job.payload["head_sha"] == "pr-sha-9"


@pytest.mark.asyncio
async def test_same_delivery_id_processed_twice_yields_exactly_one_index_job(
    migrated_engine: AsyncEngine,
) -> None:
    """US11's dedup guarantee, exercised for `INDEX` specifically: a
    redelivered `push` event with the kill switch on must not double-
    enqueue an `INDEX` job. Before this test, the only redelivered-
    delivery-id test in this file
    (`test_same_delivery_id_processed_twice_yields_exactly_one_job`) always
    ran with `indexing_enabled=False`, so it only ever exercised the
    `REVIEW`-job dedup path — `JobType.INDEX` never appeared in this file.
    """
    body = _push_payload(repo_full_name="acme/widgets", installation_id=555, after="dup-sha")

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    ) as client:
        first = _post(client, body, event_type="push", delivery_id="repeated-index-delivery")
        second = _post(client, body, event_type="push", delivery_id="repeated-index-delivery")

    assert first.status_code == 200
    assert second.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    assert jobs[0].job_type == JobType.INDEX


@pytest.mark.asyncio
async def test_index_enqueues_past_rate_limit_are_rejected_at_the_boundary(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The per-repo index-enqueue rate limit NFR
    (plan-indexing-delivery.md), the `INDEX` analogue of
    `test_enqueues_past_rate_limit_are_rejected_at_the_boundary` above for
    `REVIEW`. Before this test, `index_enqueue_rate_limiter` was only ever
    wired into a request when `indexing_enabled=True` — which no test set
    — so the index rate limiter could never actually fire in this suite.
    """
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    def _body(after: str) -> bytes:
        return _push_payload(repo_full_name="acme/widgets", installation_id=555, after=after)

    with (
        _build_client(
            dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
            indexing_enabled=True,
            index_enqueue_rate_limit_per_minute=2,
        ) as client,
        caplog.at_level("INFO"),
    ):
        first = _post(client, _body("sha-1"), event_type="push", delivery_id="idx-rl-1")
        second = _post(client, _body("sha-2"), event_type="push", delivery_id="idx-rl-2")
        third = _post(client, _body("sha-3"), event_type="push", delivery_id="idx-rl-3")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len([job for job in jobs if job.job_type == JobType.INDEX]) == 2
    assert any(
        getattr(record, "skipped_reason", None) == "index_rate_limited" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_index_rate_limit_is_shared_across_push_and_pull_request_triggers(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The per-repo `INDEX` rate-limit bucket (plan-indexing-delivery.md's
    Assumption 10 / Phase 1) is one bucket per repo covering *both*
    trigger kinds together, not a separate bucket per trigger type. Before
    this test, the only `INDEX` rate-limit test
    (`test_index_enqueues_past_rate_limit_are_rejected_at_the_boundary`,
    above) only ever sent `push` events, so a regression that accidentally
    keyed the limiter by trigger type as well as repo — defeating the
    shared-bucket design — would not have been caught.
    """
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    with (
        _build_client(
            dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
            indexing_enabled=True,
            index_enqueue_rate_limit_per_minute=2,
        ) as client,
        caplog.at_level("INFO"),
    ):
        push_response = _post(
            client,
            _push_payload(repo_full_name="acme/widgets", installation_id=123, after="sha-shared-1"),
            event_type="push",
            delivery_id="idx-shared-rl-push",
        )
        pr_response = _post(
            client,
            _payload(
                repo_full_name="acme/widgets",
                installation_id=123,
                pr_number=41,
                head_sha="pr-shared-sha",
            ),
            event_type="pull_request",
            delivery_id="idx-shared-rl-pr",
        )
        third_response = _post(
            client,
            _push_payload(repo_full_name="acme/widgets", installation_id=123, after="sha-shared-2"),
            event_type="push",
            delivery_id="idx-shared-rl-push-2",
        )

    assert push_response.status_code == 200
    assert pr_response.status_code == 200
    assert third_response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    index_jobs = [job for job in jobs if job.job_type == JobType.INDEX]
    assert len(index_jobs) == 2
    assert any(
        getattr(record, "skipped_reason", None) == "index_rate_limited" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_mixed_enqueue_and_rate_limited_outcomes_roll_back_together(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression test: a `pull_request opened` event matches both the
    `REVIEW` and `INDEX` triggers in one request. If `REVIEW` enqueues
    successfully while `INDEX` is rate-limited in that same request, the
    whole transaction — including `REVIEW`'s own successful enqueue — must
    roll back together. Before this fix, `should_commit` was
    `any_enqueued or not any_rate_limited`: since `REVIEW` did enqueue,
    the whole delivery committed anyway, durably marking it "seen" with
    `INDEX`'s own job never enqueued — permanently losing it even after
    the rate-limit window passes and the exact same delivery is
    redelivered, since the per-delivery idempotency guard has no way to
    tell "REVIEW already succeeded" apart from "this whole delivery was
    already fully handled".
    """
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    with (
        _build_client(
            dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
            indexing_enabled=True,
            index_enqueue_rate_limit_per_minute=1,
        ) as client,
        caplog.at_level("INFO"),
    ):
        # Exhausts the shared per-repo INDEX quota (limit=1) before the
        # mixed-outcome request below.
        _post(
            client,
            _push_payload(repo_full_name="acme/widgets", installation_id=555, after="sha-warmup"),
            event_type="push",
            delivery_id="mixed-warmup",
        )

        mixed_body = _payload(installation_id=555, pr_number=99, head_sha="mixed-sha")
        mixed = _post(client, mixed_body, event_type="pull_request", delivery_id="mixed-1")

        assert mixed.status_code == 200
        jobs = await _fetch_jobs(migrated_engine)
        # Only the warmup INDEX job exists — the mixed request's own REVIEW
        # enqueue was rolled back, never durably committed alongside its
        # sibling's rate-limited INDEX outcome.
        assert len(jobs) == 1
        assert jobs[0].job_type == JobType.INDEX
        assert await _delivery_count(migrated_engine) == 1  # only "mixed-warmup" recorded

        # Redelivering the exact same delivery id (still within the same
        # rate-limit window, so INDEX is rate-limited again and the whole
        # thing rolls back again too) is processed fresh, never skipped as
        # a duplicate — proving the earlier rollback never durably marked
        # it "seen". Actually clearing the rate limit to let it through
        # needs real wall-clock time to pass (a sliding window, per
        # `InMemoryRateLimiter`'s own docstring); what this fix guarantees
        # — and what's provable without waiting out that window — is that
        # the delivery stays retriable indefinitely rather than being
        # permanently resolved as "seen" after just one rate-limited pass.
        redelivered = _post(client, mixed_body, event_type="pull_request", delivery_id="mixed-1")

    assert redelivered.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    # Still just the one warmup INDEX job — redelivery rolled back again
    # too (INDEX is still rate-limited), but critically didn't lose
    # anything new, and REVIEW's own successful outcome rolled back with it
    # rather than committing while INDEX's was discarded.
    assert len(jobs) == 1
    assert await _delivery_count(migrated_engine) == 1
    assert not any(
        getattr(record, "skipped_reason", None) == "duplicate_delivery" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_mid_transaction_failure_rolls_back_both_writes(
    migrated_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forces an exception after the delivery-record insert has been staged
    on the shared session but before the outer commit — neither row should
    exist afterward, and the `warning` log should fire with the delivery
    id, proving the transaction boundary rolls both writes back together
    rather than durably marking the delivery "seen" with no job enqueued.
    """
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    async def _raising_enqueue(
        self: JobQueueRepository,
        job_type: JobType,
        payload: dict[str, object],
        *,
        org_id: uuid.UUID | None = None,
        repo_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
    ) -> uuid.UUID:
        assert session is not None
        row = JobQueueRow(
            job_type=job_type,
            status=JobStatus.PENDING,
            payload=payload,
            org_id=org_id,
            repo_id=repo_id,
        )
        session.add(row)
        await session.flush()
        raise RuntimeError("simulated failure after staging the job row")

    monkeypatch.setattr(JobQueueRepository, "enqueue", _raising_enqueue)

    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(repo_full_name="acme/widgets", installation_id=555)

    with caplog.at_level("WARNING"):
        response = _post(client, body, delivery_id="rollback-delivery")

    assert response.status_code == 500
    assert await _job_count(migrated_engine) == 0
    assert await _delivery_count(migrated_engine) == 0
    assert any(
        "enqueue transaction rolled back" in record.getMessage()
        and getattr(record, "delivery_id", None) == "rollback-delivery"
        for record in caplog.records
    )
