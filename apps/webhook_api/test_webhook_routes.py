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

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapter.storage.engine import create_session_factory
from adapter.storage.models import (
    JobQueueRow,
    JobStatus,
    JobType,
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
    WebhookDeliveryRow,
)
from adapter.storage.queue import JobQueueRepository
from apps.webhook_api.main import create_app
from apps.webhook_api.settings import WebhookAPISettings

pytestmark = pytest.mark.integration

_SECRET = "integration-test-webhook-secret"
_URL = "/webhooks/github"
_UNREACHABLE_DSN = "postgresql+asyncpg://unused:unused@localhost:5432/unused"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_EVENT_HEADER = "X-GitHub-Event"
_GITHUB_API_BASE = "https://api.github.com"
# A syntactically-arbitrary placeholder — every test below except the
# synchronous-reaction-poll ones (which override this with a real generated
# key) never actually calls `GitHubInstallationTokenProvider.get_token`, so
# nothing here ever attempts to sign a JWT with it.
_PLACEHOLDER_GITHUB_APP_PRIVATE_KEY = "test-placeholder-key-never-used-for-real-signing"


def _generate_test_rsa_private_key_pem() -> str:
    """A real (freshly generated, never a committed static blob) RSA
    private key — genuinely usable by `adapter.github.auth.sign_app_jwt`'s
    real RS256 signing path. Mirrors `adapter/github/conftest.py`'s own
    session-scoped `test_rsa_private_key_pem` fixture, which isn't visible
    to this package's own test collection tree (a different top-level
    package, not a `conftest.py` ancestor of this one) — generated once,
    module-level, rather than adding a second `conftest.py` fixture just
    for this file's one consumer.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


_TEST_RSA_PRIVATE_KEY_PEM = _generate_test_rsa_private_key_pem()


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
    secret_redaction_enabled: bool = False,
    memory_capture_ceiling_per_repo_per_day: int = 500,
    github_app_id: int = 1,
    github_app_private_key: str = _PLACEHOLDER_GITHUB_APP_PRIVATE_KEY,
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
        # `False` by default in this test module: exercises the same
        # `NoOpTextRedactor` wiring path `apps/worker/test_main.py`'s own
        # `secret_redaction_enabled=False` tests already establish, so
        # these route-level tests can assert `raw_signal_detail["body"]`
        # against the exact input text without depending on the vendored
        # Gitleaks binary's own behavior (covered elsewhere,
        # `adapter/analyzers/test_gitleaks.py`).
        secret_redaction_enabled=secret_redaction_enabled,
        memory_capture_ceiling_per_repo_per_day=memory_capture_ceiling_per_repo_per_day,
        # US-M3's second `Given/When/Then` branch (Phase 4, Unit D): backs
        # the synchronous `pull_request closed` reaction-poll path's own
        # `GitHubInstallationTokenProvider`/`GitHubReactionFetcher` — a
        # harmless placeholder by default (see
        # `_PLACEHOLDER_GITHUB_APP_PRIVATE_KEY`'s own comment), overridden
        # with a real generated key only by the tests that actually
        # exercise that path.
        github_app_id=github_app_id,
        github_app_private_key=SecretStr(github_app_private_key),
        github_api_base_url=_GITHUB_API_BASE,
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


def _review_comment_payload(
    *,
    action: str = "created",
    repo_full_name: str = "acme/widgets",
    installation_id: int = 123,
    pr_number: int = 7,
    comment_id: int = 5001,
    in_reply_to_id: int | None = 4001,
    body: str = "I don't think this applies here",
) -> bytes:
    """A `pull_request_review_comment` webhook event payload, per
    `docs/plans/plan-deliver-memory.md`'s Phase 3 section (Unit C).
    """
    comment: dict[str, object] = {"id": comment_id, "body": body}
    if in_reply_to_id is not None:
        comment["in_reply_to_id"] = in_reply_to_id
    payload: dict[str, object] = {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": repo_full_name},
        "pull_request": {"number": pr_number},
        "comment": comment,
    }
    return json.dumps(payload).encode("utf-8")


def _review_thread_payload(
    *,
    action: str = "resolved",
    repo_full_name: str = "acme/widgets",
    installation_id: int = 123,
    pr_number: int = 7,
    comment_ids: list[int] | None = None,
) -> bytes:
    """A `pull_request_review_thread` webhook event payload (action
    `resolved`/`unresolved`), per the plan's Phase 3 section.
    """
    payload: dict[str, object] = {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": repo_full_name},
        "pull_request": {"number": pr_number},
        "thread": {"comments": [{"id": comment_id} for comment_id in (comment_ids or [4001])]},
    }
    return json.dumps(payload).encode("utf-8")


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


async def _repo_id_by_full_name(engine: AsyncEngine, full_name: str) -> uuid.UUID:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(select(Repo.id).where(Repo.full_name == full_name))
        return result.scalar_one()


async def _seed_posted_comment(
    engine: AsyncEngine, *, repo_id: uuid.UUID, comment_id: int, pr_number: int = 7
) -> None:
    """Directly inserts a `posted_finding_comment` row (Phase 2's own
    output, this phase's own input) — Phase 3's capture handler correlates
    an inbound reply/thread-resolution signal's comment id against exactly
    this table, per `docs/plans/plan-deliver-memory.md`'s Phase 3 section.
    """
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=comment_id,
                repo_id=repo_id,
                pr_number=pr_number,
                commit_sha="a" * 40,
                finding_file="app/main.py",
                finding_line=10,
                finding_rule_id="no-secrets",
                finding_message="a secret was found",
            )
        )


async def _fetch_memory_entries(engine: AsyncEngine) -> list[MemoryEntryRow]:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(select(MemoryEntryRow))
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
    body = _payload(action="labeled")  # a real, harmless trigger-non-matching action
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
    # `pull_request opened` also matches `is_memory_reaction_poll_trigger`
    # (docs/plans/plan-deliver-memory.md's Phase 4 section) — a
    # `MEMORY_REACTION_POLL` job is always enqueued alongside `REVIEW` for
    # this trigger set, no kill switch of its own.
    assert len(jobs) == 2
    jobs_by_type = {job.job_type: job for job in jobs}
    assert set(jobs_by_type) == {JobType.REVIEW, JobType.MEMORY_REACTION_POLL}
    job = jobs_by_type[JobType.REVIEW]
    assert job.status == JobStatus.PENDING
    assert job.payload["installation_id"] == 555
    assert job.payload["repo_full_name"] == "acme/widgets"
    assert job.payload["pr_number"] == 1
    assert job.payload["head_sha"] == "a1"
    assert job.org_id is not None
    assert job.repo_id is not None
    reaction_poll_job = jobs_by_type[JobType.MEMORY_REACTION_POLL]
    assert reaction_poll_job.payload["pr_number"] == 1


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
    # `MEMORY_REACTION_POLL` also enqueues for this trigger — see
    # `test_unseen_installation_creates_org_repo_and_enqueues_one_job`'s own
    # comment.
    assert len(jobs) == 2
    review_job = next(job for job in jobs if job.job_type == JobType.REVIEW)
    assert review_job.payload["base_sha"] == "base-a1"


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
    # `MEMORY_REACTION_POLL` also enqueues for this trigger — see
    # `test_unseen_installation_creates_org_repo_and_enqueues_one_job`'s own
    # comment.
    assert len(jobs) == 2
    review_job = next(job for job in jobs if job.job_type == JobType.REVIEW)
    assert review_job.payload["base_sha"] is None


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
    # Two requests (`opened`, `synchronize`), each enqueuing both `REVIEW`
    # and `MEMORY_REACTION_POLL` (docs/plans/plan-deliver-memory.md's Phase
    # 4 section) -> 4 jobs total, all sharing the same org/repo.
    assert len(jobs) == 4
    assert len({job.org_id for job in jobs}) == 1
    assert len({job.repo_id for job in jobs}) == 1


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
    # One request's worth of jobs (`REVIEW` + `MEMORY_REACTION_POLL`, per
    # docs/plans/plan-deliver-memory.md's Phase 4 section) — the redelivery
    # itself enqueues nothing new.
    assert await _job_count(migrated_engine) == 2
    assert await _delivery_count(migrated_engine) == 1


@pytest.mark.asyncio
async def test_action_outside_trigger_set_accepted_but_not_enqueued(
    migrated_engine: AsyncEngine,
) -> None:
    # `"labeled"` (not `"closed"`) — since
    # docs/plans/plan-deliver-memory.md's Phase 4 section, `closed` runs a
    # synchronous reaction poll (see
    # `test_pull_request_closed_runs_synchronous_reaction_poll_and_enqueues_no_job`
    # below), never an enqueue of any kind; `"labeled"` stays genuinely
    # outside every trigger set this route knows about.
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(action="labeled", repo_full_name="acme/widgets", installation_id=555)

    response = _post(client, body, delivery_id="delivery-labeled")

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
    """`REVIEW` needs `head_sha` and so is not enqueued, but
    `MEMORY_REACTION_POLL` only needs `pr_number` (present here) — per
    docs/plans/plan-deliver-memory.md's Phase 4 section, it still enqueues."""
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))
    body = _payload(repo_full_name="acme/widgets", installation_id=555, pr_number=1, head_sha=None)

    with caplog.at_level("INFO"):
        response = _post(client, body, delivery_id="delivery-missing-sha")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 1
    assert jobs[0].job_type == JobType.MEMORY_REACTION_POLL
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
    # `review_enqueue_rate_limit_per_minute=2`: the third request's own
    # `REVIEW` candidate is rate-limited, which (per
    # `_handle_matched_triggers`'s own "the whole transaction rolls back
    # together" design) rolls back that *whole* request's transaction,
    # including its own would-be-successful `MEMORY_REACTION_POLL` enqueue —
    # so only the first two requests' pairs (`REVIEW` + `MEMORY_REACTION_POLL`
    # each, per docs/plans/plan-deliver-memory.md's Phase 4 section) land:
    # 2 * 2 = 4, not the third request's own 2 more.
    assert await _job_count(migrated_engine) == 4
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

    # Each of the `burst_size` requests enqueues both `REVIEW` and
    # `MEMORY_REACTION_POLL` (docs/plans/plan-deliver-memory.md's Phase 4
    # section — no rate limiter of its own).
    assert await _job_count(migrated_engine) == burst_size * 2


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
    enqueues the pre-existing `REVIEW` job, a new `INDEX` job, and (per
    docs/plans/plan-deliver-memory.md's Phase 4 section) a
    `MEMORY_REACTION_POLL` job, all from the same request/shared
    transaction. Before this test, no test in this file ever passed
    `indexing_enabled=True`, so `_index_enqueue_payload`'s `pull_request`
    branch never executed in any test.
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
    assert len(jobs) == 3
    jobs_by_type = {job.job_type: job for job in jobs}
    assert set(jobs_by_type) == {JobType.REVIEW, JobType.INDEX, JobType.MEMORY_REACTION_POLL}
    index_job = jobs_by_type[JobType.INDEX]
    assert index_job.payload["pr_number"] == 9
    assert index_job.payload["commit_sha"] == "pr-sha-9"
    assert index_job.payload["force_full_tree_fallback"] is True
    review_job = jobs_by_type[JobType.REVIEW]
    assert review_job.payload["pr_number"] == 9
    assert review_job.payload["head_sha"] == "pr-sha-9"
    reaction_poll_job = jobs_by_type[JobType.MEMORY_REACTION_POLL]
    assert reaction_poll_job.payload["pr_number"] == 9


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


# --- Memory reaction polling (docs/plans/plan-deliver-memory.md's Phase 4 --
# --- section, Unit D, US-M3) --------------------------------------------


@pytest.mark.asyncio
async def test_pull_request_closed_runs_synchronous_reaction_poll_and_enqueues_no_job(
    migrated_engine: AsyncEngine,
) -> None:
    """US-M3's second `Given/When/Then` branch (``spec-memory.md`` lines
    273-275): a `pull_request closed` event runs the reaction fetch
    synchronously — before the webhook handler returns its HTTP response —
    rather than enqueuing a `MEMORY_REACTION_POLL` job for the Worker to
    pick up an indeterminate time later. Since `closed` is outside
    `is_enqueue_trigger`'s/`is_index_trigger`'s own trigger sets, and (as of
    this fix) also outside the async `MEMORY_REACTION_POLL` candidate set,
    no job of any type is enqueued for this event at all — the fetch's own
    result is instead a `pending` `memory_entry` row, written directly in
    this same request.
    """
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        github_app_private_key=_TEST_RSA_PRIVATE_KEY_PEM,
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/closed-repo", installation_id=903
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4100, pr_number=13)
        jobs_before = await _fetch_jobs(migrated_engine)

        body = _payload(
            action="closed", repo_full_name="acme/closed-repo", installation_id=903, pr_number=13
        )

        with respx.mock:
            respx.post(f"{_GITHUB_API_BASE}/app/installations/903/access_tokens").mock(
                return_value=httpx.Response(
                    201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
                )
            )
            respx.get(
                f"{_GITHUB_API_BASE}/repos/acme/closed-repo/pulls/comments/4100/reactions"
            ).mock(
                return_value=httpx.Response(
                    200, json=[{"id": 9001, "content": "+1", "user": {"login": "carol"}}]
                )
            )
            response = _post(client, body, delivery_id="delivery-closed-reaction-poll")

    assert response.status_code == 200
    jobs_after = await _fetch_jobs(migrated_engine)
    # No new job of any type — the reaction fetch ran synchronously above,
    # not queued. `jobs_before` already carries `_provision_repo`'s own
    # `opened`-triggered REVIEW/MEMORY_REACTION_POLL jobs for a *different*
    # PR (7, `_payload`'s own default) — this asserts the `closed` request
    # itself added nothing further.
    assert len(jobs_after) == len(jobs_before)
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == MemoryEntryStatus.PENDING
    assert entry.raw_signal_type == "reaction"
    assert entry.posted_finding_comment_id == 4100
    assert entry.raw_signal_detail == {"reaction_id": 9001, "content": "+1"}


@pytest.mark.asyncio
async def test_pull_request_closed_missing_pr_number_is_accepted_but_not_polled(
    migrated_engine: AsyncEngine,
) -> None:
    """A `pull_request closed` payload missing `pull_request.number`
    entirely (malformed/adversarial) is still accepted (200) and never
    attempts the synchronous reaction-fetch call — mirrors
    `_memory_reaction_poll_payload`'s own `event.pr_number is None` guard
    for the async-job path."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        github_app_private_key=_TEST_RSA_PRIVATE_KEY_PEM,
    ) as client:
        body = _payload(
            action="closed",
            repo_full_name="acme/no-pr-number-repo",
            installation_id=904,
            pr_number=None,
        )

        with respx.mock:
            # No mocks registered: any attempted call raises inside respx,
            # failing this test loudly if the guard above doesn't hold.
            response = _post(client, body, delivery_id="delivery-closed-no-pr-number")

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert entries == []


@pytest.mark.asyncio
async def test_pull_request_synchronize_enqueues_all_three_job_types(
    migrated_engine: AsyncEngine,
) -> None:
    """US-M3's own Acceptance wording: "a `pull_request synchronize` event
    enqueues all three job types from one shared transaction" — with the
    indexing kill switch on, so `INDEX` is actually eligible to enqueue
    too."""
    client = _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        indexing_enabled=True,
    )
    body = _payload(
        action="synchronize",
        repo_full_name="acme/widgets",
        installation_id=555,
        pr_number=14,
        head_sha="sync-sha-14",
    )

    response = _post(client, body, delivery_id="delivery-sync-all-three")

    assert response.status_code == 200
    jobs = await _fetch_jobs(migrated_engine)
    assert len(jobs) == 3
    assert {job.job_type for job in jobs} == {
        JobType.REVIEW,
        JobType.INDEX,
        JobType.MEMORY_REACTION_POLL,
    }
    # All three landed from the same shared transaction — one org/repo pair.
    assert len({job.org_id for job in jobs}) == 1
    assert len({job.repo_id for job in jobs}) == 1


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


# --- Memory capture: replies and thread-resolution ---
# (docs/plans/plan-deliver-memory.md's Phase 3, Unit C, US-M1/US-M2)


async def _provision_repo(
    client: TestClient, engine: AsyncEngine, *, repo_full_name: str, installation_id: int
) -> uuid.UUID:
    """Auto-provisions org/repo for `repo_full_name`/`installation_id` via
    an ordinary `pull_request opened` webhook — the same auto-provisioning
    path every other test in this module already exercises — then resolves
    the real `repo_id` this phase's own tests need to seed a
    `posted_finding_comment` row against.
    """
    body = _payload(repo_full_name=repo_full_name, installation_id=installation_id)
    response = _post(client, body, delivery_id=f"provision-{repo_full_name}-{installation_id}")
    assert response.status_code == 200
    return await _repo_id_by_full_name(engine, repo_full_name)


@pytest.mark.asyncio
async def test_reply_to_posted_comment_creates_pending_memory_entry(
    migrated_engine: AsyncEngine,
) -> None:
    """US-M1's Given/When/Then: a `pull_request_review_comment` `created`
    event whose `comment.in_reply_to_id` matches a `posted_finding_comment`
    row creates a `pending`, unclassified `memory_entry` capturing the
    (redacted) reply text, provenance-linked to that row.
    """
    # `with` (not a bare `client = ...`): every request below shares one
    # TestClient portal/event loop — a second, separately-established
    # portal per bare `.post()` call would leave the app's own internal
    # engine's asyncpg connection pool straddling two different event
    # loops, per starlette's own `TestClient` contract.
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/reply-repo", installation_id=901
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4001)

        body = _review_comment_payload(
            repo_full_name="acme/reply-repo",
            installation_id=901,
            comment_id=5001,
            in_reply_to_id=4001,
            body="I don't think this applies here",
        )

        response = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="reply-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == MemoryEntryStatus.PENDING
    assert entry.entry_type is None
    assert entry.raw_signal_type == "reply"
    assert entry.raw_signal_detail == {"body": "I don't think this applies here"}
    assert entry.posted_finding_comment_id == 4001
    assert entry.github_comment_id == 5001


@pytest.mark.asyncio
async def test_reply_with_no_in_reply_to_id_is_accepted_but_not_captured(
    migrated_engine: AsyncEngine,
) -> None:
    """Assumption 3: a `created` comment with `in_reply_to_id=None` is a
    fresh top-level review comment, not a reply, and is never captured —
    still accepted (200)."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/fresh-comment-repo", installation_id=902
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4002)

        body = _review_comment_payload(
            repo_full_name="acme/fresh-comment-repo",
            installation_id=902,
            comment_id=5002,
            in_reply_to_id=None,
            body="a fresh top-level review comment",
        )

        response = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="fresh-comment-1"
        )

    assert response.status_code == 200
    assert await _fetch_memory_entries(migrated_engine) == []


@pytest.mark.asyncio
async def test_reply_to_unknown_comment_is_accepted_but_not_captured(
    migrated_engine: AsyncEngine,
) -> None:
    """A reply whose `in_reply_to_id` doesn't correlate to any
    `posted_finding_comment` row (not one this platform posted) is accepted
    (200) and logged, never captured — the "no match" case both Phase 3's
    plan section and US-M1 describe."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        await _provision_repo(
            client, migrated_engine, repo_full_name="acme/no-match-repo", installation_id=903
        )

        body = _review_comment_payload(
            repo_full_name="acme/no-match-repo",
            installation_id=903,
            comment_id=5003,
            in_reply_to_id=999_999,
            body="replying to something the platform never posted",
        )

        response = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="no-match-1"
        )

    assert response.status_code == 200
    assert await _fetch_memory_entries(migrated_engine) == []


@pytest.mark.asyncio
async def test_thread_resolved_creates_pending_memory_entry(
    migrated_engine: AsyncEngine,
) -> None:
    """US-M2's Given/When/Then, `resolved` branch."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/thread-repo", installation_id=904
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4004)

        body = _review_thread_payload(
            action="resolved",
            repo_full_name="acme/thread-repo",
            installation_id=904,
            comment_ids=[4004],
        )

        response = _post(
            client, body, event_type="pull_request_review_thread", delivery_id="thread-resolved-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == MemoryEntryStatus.PENDING
    assert entry.raw_signal_type == "thread_resolution"
    assert entry.raw_signal_detail == {"resolved": True}
    assert entry.posted_finding_comment_id == 4004


@pytest.mark.asyncio
async def test_thread_unresolved_creates_pending_memory_entry(
    migrated_engine: AsyncEngine,
) -> None:
    """US-M2's Given/When/Then, `unresolved` branch."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client,
            migrated_engine,
            repo_full_name="acme/thread-unresolved-repo",
            installation_id=905,
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4005)

        body = _review_thread_payload(
            action="unresolved",
            repo_full_name="acme/thread-unresolved-repo",
            installation_id=905,
            comment_ids=[4005],
        )

        response = _post(
            client, body, event_type="pull_request_review_thread", delivery_id="thread-unresolved-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    assert entries[0].raw_signal_detail == {"resolved": False}


@pytest.mark.asyncio
async def test_thread_resolution_matches_any_comment_id_in_the_thread(
    migrated_engine: AsyncEngine,
) -> None:
    """`thread.comments` can carry ids beyond the platform's own posted
    comment (other reviewers' replies in the same thread) — any id in the
    array that correlates to a `posted_finding_comment` row is a match."""
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/thread-multi-repo", installation_id=906
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4006)

        body = _review_thread_payload(
            action="resolved",
            repo_full_name="acme/thread-multi-repo",
            installation_id=906,
            comment_ids=[9999, 4006, 8888],  # only 4006 is a platform-posted comment
        )

        response = _post(
            client, body, event_type="pull_request_review_thread", delivery_id="thread-multi-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == 4006
    # `github_comment_id` is the thread's own last comment id, independent
    # of which id in the thread actually matched.
    assert entries[0].github_comment_id == 8888


@pytest.mark.asyncio
async def test_thread_resolution_records_every_matching_comment_in_the_thread(
    migrated_engine: AsyncEngine,
) -> None:
    """A single resolved/unresolved thread can carry more than one of the
    platform's own posted findings (e.g. two separate review passes
    attaching comments to the same line/thread) — every id in
    `thread.comments` that correlates to a `posted_finding_comment` row
    must get its own `thread_resolution` signal, not just the first match.
    """
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/thread-double-repo", installation_id=910
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4011)
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4012)

        body = _review_thread_payload(
            action="resolved",
            repo_full_name="acme/thread-double-repo",
            installation_id=910,
            comment_ids=[4011, 4012],  # both are platform-posted comments
        )

        response = _post(
            client, body, event_type="pull_request_review_thread", delivery_id="thread-double-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert {entry.posted_finding_comment_id for entry in entries} == {4011, 4012}


@pytest.mark.asyncio
async def test_thread_resolution_with_no_matching_comment_is_accepted_but_not_captured(
    migrated_engine: AsyncEngine,
) -> None:
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        await _provision_repo(
            client,
            migrated_engine,
            repo_full_name="acme/thread-no-match-repo",
            installation_id=907,
        )

        body = _review_thread_payload(
            action="resolved",
            repo_full_name="acme/thread-no-match-repo",
            installation_id=907,
            comment_ids=[111, 222],
        )

        response = _post(
            client, body, event_type="pull_request_review_thread", delivery_id="thread-no-match-1"
        )

    assert response.status_code == 200
    assert await _fetch_memory_entries(migrated_engine) == []


@pytest.mark.asyncio
async def test_redelivered_reply_does_not_create_a_duplicate_entry(
    migrated_engine: AsyncEngine,
) -> None:
    """Redelivery-safety (plan's Phase 3 section): even across two distinct
    `X-GitHub-Delivery` ids (so the job-queue path's own delivery-id guard
    never applies here), the exact same reply signal
    (`posted_finding_comment_id`/`raw_signal_type`/`github_comment_id`)
    only ever produces one `memory_entry` row, via `record_signal`'s own
    `ON CONFLICT DO NOTHING` upsert.
    """
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/redelivery-repo", installation_id=908
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4008)

        body = _review_comment_payload(
            repo_full_name="acme/redelivery-repo",
            installation_id=908,
            comment_id=5008,
            in_reply_to_id=4008,
            body="redelivered reply",
        )

        first = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="redelivery-1"
        )
        second = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="redelivery-2"
        )

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await _fetch_memory_entries(migrated_engine)) == 1


@pytest.mark.asyncio
async def test_thread_toggle_between_resolved_and_unresolved_captures_both(
    migrated_engine: AsyncEngine,
) -> None:
    """A thread `resolved` event followed by a later `unresolved` event on
    that *same* thread are two distinct real-world state changes, not a
    redelivery of the same event — both must be captured. Regression test
    for a review-pass finding on the first Phase 3 implementation: the
    route always used `raw_signal_type="thread_resolution"` and
    `github_comment_id=comment_ids[-1]` regardless of `action`, so the
    redelivery-safety `ON CONFLICT DO NOTHING` upsert silently discarded
    the second (`unresolved`) event's write, even though `record_signal`
    still reported `found=True` for it.
    """
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/thread-toggle-repo", installation_id=910
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4011)

        resolved_body = _review_thread_payload(
            action="resolved",
            repo_full_name="acme/thread-toggle-repo",
            installation_id=910,
            comment_ids=[4011],
        )
        unresolved_body = _review_thread_payload(
            action="unresolved",
            repo_full_name="acme/thread-toggle-repo",
            installation_id=910,
            comment_ids=[4011],
        )

        resolved_response = _post(
            client,
            resolved_body,
            event_type="pull_request_review_thread",
            delivery_id="thread-toggle-resolved",
        )
        unresolved_response = _post(
            client,
            unresolved_body,
            event_type="pull_request_review_thread",
            delivery_id="thread-toggle-unresolved",
        )

    assert (resolved_response.status_code, unresolved_response.status_code) == (200, 200)
    entries = await _fetch_memory_entries(migrated_engine)
    resolved_values = {entry.raw_signal_detail["resolved"] for entry in entries}
    assert resolved_values == {True, False}
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_reply_with_redaction_enabled_redacts_secret_before_persisting(
    migrated_engine: AsyncEngine,
) -> None:
    """The plan's Phase 3 Tests subsection explicitly requires this
    scenario: a reply's raw body is redacted (via the real Gitleaks-backed
    `TextRedactor`, not `NoOpTextRedactor`) before being persisted as
    `raw_signal_detail`. Every other reply test in this module uses
    `_build_client`'s `secret_redaction_enabled=False` default (see that
    fixture's own docstring) and so only proves pass-through behavior; this
    is the one test that exercises the real redaction wiring end to end.
    """
    planted_secret = 'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"'
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        secret_redaction_enabled=True,
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/redact-reply-repo", installation_id=911
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4012)

        body = _review_comment_payload(
            repo_full_name="acme/redact-reply-repo",
            installation_id=911,
            comment_id=5012,
            in_reply_to_id=4012,
            body=planted_secret,
        )

        response = _post(
            client, body, event_type="pull_request_review_comment", delivery_id="redact-reply-1"
        )

    assert response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    redacted_body = entries[0].raw_signal_detail["body"]
    assert isinstance(redacted_body, str)
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz12" not in redacted_body
    assert redacted_body != planted_secret


@pytest.mark.asyncio
async def test_capture_ceiling_exceeded_is_accepted_but_not_persisted(
    migrated_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-repo daily cap on capture writes — a real gap this plan's own
    review pass found (any GitHub account able to comment on a public
    repo's PR can otherwise generate unbounded `pending` `memory_entry`
    rows, no privileged permission required). Exceeding it is still
    accepted (200), matching this route's own "never reject a well-formed
    signed webhook" posture, but not persisted.
    """
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        memory_capture_ceiling_per_repo_per_day=1,
    ) as client:
        repo_id = await _provision_repo(
            client, migrated_engine, repo_full_name="acme/ceiling-repo", installation_id=909
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4009)
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4010)

        first_body = _review_comment_payload(
            repo_full_name="acme/ceiling-repo",
            installation_id=909,
            comment_id=5009,
            in_reply_to_id=4009,
            body="first reply, within the ceiling",
        )
        second_body = _review_comment_payload(
            repo_full_name="acme/ceiling-repo",
            installation_id=909,
            comment_id=5010,
            in_reply_to_id=4010,
            body="second reply, past the ceiling",
        )

        with caplog.at_level("WARNING"):
            first = _post(
                client,
                first_body,
                event_type="pull_request_review_comment",
                delivery_id="ceiling-1",
            )
            second = _post(
                client,
                second_body,
                event_type="pull_request_review_comment",
                delivery_id="ceiling-2",
            )

    assert (first.status_code, second.status_code) == (200, 200)
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == 4009
    assert any(
        getattr(record, "reason", None) == "capture_ceiling_exceeded" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_non_reply_comments_do_not_consume_the_capture_ceiling(
    migrated_engine: AsyncEngine,
) -> None:
    """A fresh top-level review comment (not a reply, `in_reply_to_id` is
    absent) is never a capture candidate at all — it must not burn the
    per-repo daily ceiling that's meant to bound genuine capture attempts.
    With the ceiling set to 1, any number of non-reply comments must not
    prevent a real reply that follows from being captured.
    """
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False)),
        memory_capture_ceiling_per_repo_per_day=1,
    ) as client:
        repo_id = await _provision_repo(
            client,
            migrated_engine,
            repo_full_name="acme/non-reply-ceiling-repo",
            installation_id=911,
        )
        await _seed_posted_comment(migrated_engine, repo_id=repo_id, comment_id=4013)

        for i in range(3):
            non_reply_body = _review_comment_payload(
                repo_full_name="acme/non-reply-ceiling-repo",
                installation_id=911,
                comment_id=6000 + i,
                in_reply_to_id=None,
                body=f"an ordinary top-level review comment {i}",
            )
            response = _post(
                client,
                non_reply_body,
                event_type="pull_request_review_comment",
                delivery_id=f"non-reply-{i}",
            )
            assert response.status_code == 200

        reply_body = _review_comment_payload(
            repo_full_name="acme/non-reply-ceiling-repo",
            installation_id=911,
            comment_id=6100,
            in_reply_to_id=4013,
            body="a genuine reply, still within the ceiling",
        )
        reply_response = _post(
            client,
            reply_body,
            event_type="pull_request_review_comment",
            delivery_id="non-reply-reply",
        )

    assert reply_response.status_code == 200
    entries = await _fetch_memory_entries(migrated_engine)
    assert len(entries) == 1
    assert entries[0].posted_finding_comment_id == 4013
