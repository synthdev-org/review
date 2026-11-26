"""Unit tests for tenancy.resolution's business rules, per
docs/rules/testing.md's "new domain logic a story touches still needs unit
tests" rule (``tenancy`` is one of testing.md's explicitly-listed Unit-tier
domain packages). Fakes at the ``TenancyStore`` Protocol boundary, no real
I/O — constructs ``WebhookEvent`` instances directly, never
``adapter.github.schemas.GitHubWebhookEnvelope``, proving this package's
own test suite needs no adapter import either.

These are the same scenarios ``apps/webhook_api/test_webhook_routes.py``'s
Integration tests prove end-to-end through a real Postgres — this module
proves the decision logic itself in isolation, per testing.md's "necessary,
not sufficient" framing for domain logic a story touches.
"""

from __future__ import annotations

import uuid

import pytest

from tenancy.resolution import OrgRepoIds, WebhookEvent, resolve_installation, split_owner_repo


class _FakeTenancyStore:
    """In-memory fake ``TenancyStore``. Tracks every call
    (``get_or_create_calls``) separately from genuine creations
    (``created_keys``) so tests can assert "resolved an existing row"
    without accidentally also proving "created a new one" (or vice versa).
    """

    def __init__(self) -> None:
        self.get_or_create_calls: list[tuple[int, str]] = []
        self.created_keys: list[tuple[int, str]] = []
        self._org_repos: dict[tuple[int, str], OrgRepoIds] = {}
        self._deliveries: set[str] = set()

    async def get_or_create_org_repo(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        unit_of_work: object | None = None,
    ) -> OrgRepoIds:
        key = (installation_id, repo_full_name)
        self.get_or_create_calls.append(key)
        if key not in self._org_repos:
            self._org_repos[key] = OrgRepoIds(org_id=uuid.uuid4(), repo_id=uuid.uuid4())
            self.created_keys.append(key)
        return self._org_repos[key]

    async def record_delivery_if_new(
        self, delivery_id: str, *, unit_of_work: object | None = None
    ) -> bool:
        if delivery_id in self._deliveries:
            return False
        self._deliveries.add(delivery_id)
        return True


def _event(**overrides: object) -> WebhookEvent:
    defaults: dict[str, object] = {
        "delivery_id": "delivery-1",
        "installation_id": 123,
        "action": "opened",
        "repo_full_name": "acme/widgets",
        "pr_number": 7,
        "head_sha": "deadbeefcafe",
    }
    defaults.update(overrides)
    return WebhookEvent.model_validate(defaults)


@pytest.mark.asyncio
async def test_first_sight_auto_provisions_a_fresh_org_repo() -> None:
    store = _FakeTenancyStore()

    result = await resolve_installation(_event(), store)

    assert store.created_keys == [(123, "acme/widgets")]
    assert result.org_id is not None
    assert result.repo_id is not None
    assert result.is_new_delivery is True


@pytest.mark.asyncio
async def test_known_installation_resolves_existing_rows_without_a_create() -> None:
    store = _FakeTenancyStore()
    first = await resolve_installation(_event(delivery_id="delivery-1"), store)

    second = await resolve_installation(_event(delivery_id="delivery-2"), store)

    assert second.org_id == first.org_id
    assert second.repo_id == first.repo_id
    # Two get_or_create_org_repo calls, but only the first one created rows.
    assert store.get_or_create_calls == [(123, "acme/widgets"), (123, "acme/widgets")]
    assert store.created_keys == [(123, "acme/widgets")]


@pytest.mark.asyncio
async def test_duplicate_delivery_id_detected_via_the_store_without_a_second_create() -> None:
    store = _FakeTenancyStore()
    first = await resolve_installation(_event(delivery_id="dup"), store)

    second = await resolve_installation(_event(delivery_id="dup"), store)

    assert first.is_new_delivery is True
    assert second.is_new_delivery is False
    assert store.created_keys == [(123, "acme/widgets")]


@pytest.mark.asyncio
async def test_different_installations_resolve_to_different_org_repo_ids() -> None:
    store = _FakeTenancyStore()

    first = await resolve_installation(
        _event(delivery_id="d1", installation_id=1, repo_full_name="acme/one"), store
    )
    second = await resolve_installation(
        _event(delivery_id="d2", installation_id=2, repo_full_name="acme/two"), store
    )

    assert first.org_id != second.org_id or first.repo_id != second.repo_id
    assert store.created_keys == [(1, "acme/one"), (2, "acme/two")]


def test_split_owner_repo_splits_on_the_first_slash() -> None:
    assert split_owner_repo("acme/widgets") == ("acme", "widgets")
    assert split_owner_repo("acme/widgets/nested") == ("acme", "widgets/nested")


@pytest.mark.parametrize(
    "malformed",
    ["no-slash-at-all", "owner/", "/repo", ""],
)
def test_split_owner_repo_rejects_a_malformed_repo_full_name(malformed: str) -> None:
    with pytest.raises(ValueError, match="not in 'owner/repo' form"):
        split_owner_repo(malformed)
