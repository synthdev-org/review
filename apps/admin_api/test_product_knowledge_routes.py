"""
Integration tests for ``product_knowledge_routes.py``: status classes (US-PK10).

See ``pk_routes_harness`` for the shared app/provider harness (A2).
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.product_knowledge.testing.fake_provider import (
    ERROR_BODY_CANARY,
    HOSTILE_TEXT,
    Era,
    FakeProvider,
    Fault,
)
from apps.admin_api.product_knowledge_routes import (
    _MESSAGES,
    ProbeStatus,
)
from apps.admin_api.testing.pk_routes_harness import (
    TOKEN,
    Harness,
    dsn,
    post,
    run,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# --- US-PK10: the status classes against the fake provider ---------------------


@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_a_conforming_provider_is_ok_with_only_validated_fields(
    migrated_engine: AsyncEngine, era: Era
) -> None:
    async with FakeProvider(era=era, token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider)

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["negotiated_version"] == "1"
    assert body["provider_supported_versions"] == ["1"]
    assert set(body["tools_present"]) <= {
        "product_knowledge_contract",
        "product_knowledge_search",
        "product_knowledge_fetch",
        "product_knowledge_related",
    }
    assert isinstance(body["latency_ms"], int)
    # The probe sent the real token to the bound host only.
    assert {r.authorization for r in provider.requests} == {f"Bearer {TOKEN}"}
    assert TOKEN not in response.text
    # The fixed probe query, one result requested.
    searches = [r for r in provider.requests if r.tool == "product_knowledge_search"]
    assert searches and searches[0].arguments is not None
    assert searches[0].arguments["query"] == "connection test"
    assert searches[0].arguments["top_k"] == 1


_STATUS_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("auth_failed", {"token": "a-different-token"}, "auth_failed"),
    ("no_contract", {"omit_tools": frozenset({"product_knowledge_contract"})}, "no_contract"),
    ("unsupported_version", {"supported_versions": ["99"]}, "unsupported_version"),
    (
        "malformed_contract",
        {"fault": Fault("bad_structure", tool="product_knowledge_contract")},
        "malformed",
    ),
    (
        "malformed_search",
        {"fault": Fault("malformed", tool="product_knowledge_search")},
        "malformed",
    ),
    ("provider_5xx", {"fault": Fault("status500", tool="product_knowledge_search")}, "unreachable"),
    ("hang", {"fault": Fault("hang", tool="product_knowledge_search")}, "timeout"),
    (
        "redirect",
        {"fault": Fault("redirect", tool="product_knowledge_search")},
        "blocked_by_policy",
    ),
]


@pytest.mark.parametrize(("name", "provider_kwargs", "expected"), _STATUS_CASES)
async def test_each_provider_failure_class_maps_to_its_status_with_no_extra_fields(
    migrated_engine: AsyncEngine, name: str, provider_kwargs: dict[str, Any], expected: str
) -> None:
    kwargs = {"token": TOKEN, **provider_kwargs}
    async with FakeProvider(**kwargs) as provider:
        h = Harness(dsn(migrated_engine), provider)

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200, name
    body = response.json()
    assert body["status"] == expected, name
    assert body["status"] in get_args(ProbeStatus), name
    # Only a conforming response echoes protocol details or timing.
    for field in (
        "negotiated_version",
        "provider_supported_versions",
        "tools_present",
        "latency_ms",
    ):
        assert body[field] is None, (name, field)


async def test_a_non_conforming_supported_versions_entry_is_malformed_and_never_echoed(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN, supported_versions=["1", "<script>"]) as provider:
        h = Harness(dsn(migrated_engine), provider)

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "malformed"
    assert "<script>" not in response.text
    assert body["provider_supported_versions"] is None
    assert body["negotiated_version"] is None


async def test_a_deprecated_major_is_reported_as_deprecated_version_within_the_closed_set(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider, deprecated_majors={"1"})

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "deprecated_version"
    assert body["negotiated_version"] == "1"  # still works: fields are reported
    # Closed set: every status the API can emit has a review-generated message.
    assert set(get_args(ProbeStatus)) == set(_MESSAGES)


async def test_provider_text_is_never_echoed_in_the_message(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider(
        token=TOKEN, fault=Fault("error_result", tool="product_knowledge_search")
    ) as provider:
        h = Harness(dsn(migrated_engine), provider)
        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )
    async with FakeProvider(token=TOKEN, fault=Fault("hostile_metadata")) as hostile:
        h2 = Harness(dsn(migrated_engine), hostile)
        response2 = await run(
            h2, lambda c, csrf: post(c, csrf, endpoint_url=h2.endpoint, credential_ref="wiki")
        )

    for r in (response, response2):
        assert r.status_code == 200
        assert ERROR_BODY_CANARY not in r.text
        assert HOSTILE_TEXT not in r.text
        assert TOKEN not in r.text


async def test_a_loopback_resolution_is_refused_by_the_egress_guard_before_any_bytes(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider, resolve_to="127.0.0.1")

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.json()["status"] == "blocked_by_policy"
    assert response.json()["latency_ms"] is None
    assert h.bytes_sent == 0
    assert h.pinned_hosts == []
