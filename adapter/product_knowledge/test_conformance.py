"""Integration tests for adapter.product_knowledge.conformance (plan Phase 5,
US-PK17): the suite passes against the in-repo fake provider (both protocol
eras) and *fails* against a seeded-faulty fake for every check, proving each
check can fail. ``test_live_endpoint_conforms`` is the opt-in live mode
(``--pk-url`` / ``PK_URL``, token in ``PK_TOKEN``); it is skipped by default and
is never a CI gate (testing.md).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from pydantic import SecretStr

from adapter.product_knowledge.conformance import (
    CheckResult,
    ConformanceTarget,
    Status,
    main,
    run_conformance,
)
from adapter.product_knowledge.testing.fake_provider import (
    Era,
    FakeProvider,
    Fault,
    default_items,
)
from review.product_knowledge import TOOL_CONTRACT, TOOL_FETCH, TOOL_RELATED, TOOL_SEARCH

pytestmark = pytest.mark.integration

TOKEN = "tok-CONFORMANCE-0123456789"

CHECK_NAMES = [
    "contract_tool_present",
    "contract_declaration_valid",
    "version_1_declared",
    "search_schema",
    "search_titles",
    "search_ordering",
    "search_limits",
    "top_k_honored",
    "fetch_returned_id",
    "fetch_unknown_id_not_found",
    "unsupported_version_rejected",
    "related_shape",
    "response_size_within_caps",
    "auth_rejects_missing_and_invalid_token",
]


async def _run(provider: FakeProvider, token: str | None = None) -> dict[str, CheckResult]:
    target = ConformanceTarget(
        url=provider.url,
        token=SecretStr(token) if token else None,
        query="billing retry",
        timeout_seconds=5.0,
    )
    results = await run_conformance(target)
    return {r.name: r for r in results}


def _items(**override: Any) -> list[dict[str, Any]]:
    return [dict(item, **override) for item in default_items(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_suite_passes_on_the_fake_in_both_eras(era: Era) -> None:
    async with FakeProvider(era=era, token=TOKEN) as provider:
        results = await _run(provider, token=TOKEN)

    assert list(results) == CHECK_NAMES
    assert {n: r.status for n, r in results.items()} == dict.fromkeys(CHECK_NAMES, Status.PASS)


@pytest.mark.asyncio
async def test_optional_checks_skip_when_not_applicable() -> None:
    async with FakeProvider(with_related=False) as provider:
        results = await _run(provider, token=None)

    assert results["related_shape"].status is Status.SKIP
    assert results["auth_rejects_missing_and_invalid_token"].status is Status.SKIP
    assert not [r for r in results.values() if r.status is Status.FAIL]


def _no_title() -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k != "title"} for i in default_items(2)]


# One seeded-faulty fake per check: (check that must fail, provider kwargs).
FAULTY: list[tuple[str, dict[str, Any]]] = [
    ("contract_tool_present", {"omit_tools": frozenset({TOOL_CONTRACT})}),
    ("contract_declaration_valid", {"fault": Fault("bad_structure", tool=TOOL_CONTRACT)}),
    ("version_1_declared", {"supported_versions": ["2"]}),
    ("search_schema", {"fault": Fault("bad_structure", tool=TOOL_SEARCH)}),
    ("search_titles", {"items": _no_title()}),
    ("search_titles", {"items": _items(title="   ")}),
    (
        "search_ordering",
        {"items": [dict(default_items(2)[0], score=0.1), dict(default_items(2)[1], score=0.9)]},
    ),
    ("search_limits", {"items": _items(snippet="x" * 1001)}),
    ("search_limits", {"items": _items(title="t" * 201)}),
    ("search_limits", {"items": _items(url="https://wiki.example.com/" + "u" * 2048)}),
    (
        "search_limits",  # more results than top_k=TOP_K
        {"items": default_items(6), "fault": Fault("ignore_top_k", tool=TOOL_SEARCH)},
    ),
    ("top_k_honored", {"fault": Fault("ignore_top_k", tool=TOOL_SEARCH)}),
    ("fetch_returned_id", {"fault": Fault("bad_structure", tool=TOOL_FETCH)}),
    ("fetch_returned_id", {"fault": Fault("fetch_wrong_id", tool=TOOL_FETCH)}),
    ("fetch_returned_id", {"fault": Fault("fetch_long_content", tool=TOOL_FETCH)}),
    ("fetch_unknown_id_not_found", {"fault": Fault("fetch_any", tool=TOOL_FETCH)}),
    ("unsupported_version_rejected", {"fault": Fault("accept_any_version", tool=TOOL_SEARCH)}),
    ("related_shape", {"fault": Fault("bad_structure", tool=TOOL_RELATED)}),
    ("response_size_within_caps", {"fault": Fault("huge", tool=TOOL_SEARCH)}),
]


def test_every_check_has_a_seeded_faulty_fake() -> None:
    assert {name for name, _ in FAULTY} | {"auth_rejects_missing_and_invalid_token"} == set(
        CHECK_NAMES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("check", "kwargs"), FAULTY, ids=[f"{n}-{i}" for i, (n, _) in enumerate(FAULTY)]
)
async def test_each_check_fails_on_its_seeded_faulty_fake(
    check: str, kwargs: dict[str, Any]
) -> None:
    async with FakeProvider(**kwargs) as provider:
        results = await _run(provider)

    assert results[check].status is Status.FAIL, results[check]
    assert results[check].detail


@pytest.mark.asyncio
async def test_auth_check_fails_when_the_server_does_not_enforce_its_token() -> None:
    async with FakeProvider(token=None) as provider:  # claims nothing, enforces nothing
        results = await _run(provider, token=TOKEN)

    assert results["auth_rejects_missing_and_invalid_token"].status is Status.FAIL


@pytest.mark.asyncio
async def test_unreachable_endpoint_is_one_failed_connect_check_not_a_crash() -> None:
    target = ConformanceTarget(url="http://127.0.0.1:1/mcp", timeout_seconds=2.0)

    results = await run_conformance(target)

    assert [(r.name, r.status) for r in results[:1]] == [("connect", Status.FAIL)]


@pytest.mark.asyncio
async def test_token_and_provider_content_never_appear_in_results() -> None:
    async with FakeProvider(token=TOKEN, items=_items(snippet="CANARY_SNIPPET " + "y" * 1100)) as p:
        results = await _run(p, token=TOKEN)

    rendered = " ".join(r.detail for r in results.values())
    assert TOKEN not in rendered
    assert "CANARY_SNIPPET" not in rendered


@pytest.mark.asyncio
async def test_cli_exit_code_and_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PK_TOKEN", TOKEN)
    async with FakeProvider(token=TOKEN) as good:
        ok = await asyncio.to_thread(main, ["--pk-url", good.url, "--pk-query", "billing retry"])
    out = capsys.readouterr().out
    async with FakeProvider(token=TOKEN, items=_no_title()) as bad:
        failed = await asyncio.to_thread(main, ["--pk-url", bad.url, "--pk-query", "billing retry"])
    bad_out = capsys.readouterr().out

    assert ok == 0 and "FAIL" not in out
    assert failed == 1 and "FAIL search_titles" in bad_out
    assert TOKEN not in out + bad_out


@pytest.mark.asyncio
async def test_live_endpoint_conforms(request: pytest.FixtureRequest) -> None:
    url = request.config.getoption("--pk-url")
    if not url:
        pytest.skip("live mode is opt-in: pass --pk-url (token in PK_TOKEN)")
    token = os.environ.get("PK_TOKEN", "").strip()
    target = ConformanceTarget(
        url=str(url),
        token=SecretStr(token) if token else None,
        query=str(request.config.getoption("--pk-query")),
    )

    results = await run_conformance(target)

    failures = [f"{r.name}: {r.detail}" for r in results if r.status is Status.FAIL]
    assert not failures, "\n".join(failures)


@pytest.mark.asyncio
async def test_search_ordering_says_so_when_there_are_no_scores_to_order() -> None:
    unscored = [{k: v for k, v in i.items() if k != "score"} for i in default_items(2)]
    async with FakeProvider(items=unscored) as provider:
        results = await _run(provider)

    assert results["search_ordering"].status is Status.SKIP
    assert "no scores to order" in results["search_ordering"].detail
