"""Tests for adapter.github.posting — the concrete FindingsPublisher
implementation, per docs/rules/testing.md's "GitHub adapter against a
recorded cassette (respx for httpx)" convention and the plan's Phase 7
Tests section.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import GitHubPostingUnreachableError
from adapter.github.posting import GitHubFindingsPublisher
from findings.models import Finding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_BASE = "https://api.github.com"


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _publisher(
    test_rsa_private_key_pem: str, *, max_retry_attempts: int = 3
) -> GitHubFindingsPublisher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubFindingsPublisher(token_provider=provider, max_retry_attempts=max_retry_attempts)


def _finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "file": "app.py",
        "line": 10,
        "severity": "high",
        "message": "hardcoded secret",
        "source": "gitleaks",
    }
    defaults.update(overrides)
    return Finding.model_validate(defaults)


# --- post_review: batched comments, markdown escaping ----------------------


@respx.mock
async def test_post_review_sends_one_batched_review_with_every_comment(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    findings = [
        _finding(file="app.py", line=10, message="secret A"),
        _finding(file="lib.py", line=20, message="secret B", source="semgrep"),
    ]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        pr_number=_PR_NUMBER,
        findings=findings,
    )

    assert route.call_count == 1
    body = route.calls.last.request.content
    payload = json.loads(body)
    assert payload["event"] == "COMMENT"
    assert len(payload["comments"]) == 2
    assert {c["path"] for c in payload["comments"]} == {"app.py", "lib.py"}


@respx.mock
async def test_post_review_escapes_untrusted_finding_text(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    findings = [_finding(message="click [here](https://evil.example) @someone")]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        pr_number=_PR_NUMBER,
        findings=findings,
    )

    sent_body = route.calls.last.request.content.decode()
    assert "[here](https://evil.example)" not in sent_body
    assert "@someone" not in sent_body


@respx.mock
async def test_post_review_appends_citation_markdown_unescaped(
    test_rsa_private_key_pem: str,
) -> None:
    """`Finding.citation_markdown` (`review.citation.render_citation`'s own
    already-safe-to-post output) must reach GitHub verbatim -- never passed
    through `escape_markdown` a second time, which would backslash-escape
    its own Markdown template (regression test for the citation-escaping
    bug fixed alongside this test). `message` is still escaped as normal."""
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    findings = [
        _finding(
            message="some_message",
            citation_markdown="**Context:**\n- `src/caller.ts:10-12` (`caller`)",
        )
    ]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        pr_number=_PR_NUMBER,
        findings=findings,
    )

    payload = json.loads(route.calls.last.request.content)
    comment_body = payload["comments"][0]["body"]
    assert "**Context:**\n- `src/caller.ts:10-12` (`caller`)" in comment_body
    assert "some\\_message" in comment_body


@respx.mock
async def test_post_review_puts_file_level_finding_in_body_not_comments(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    findings = [_finding(line=None, message="repo-wide issue")]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        pr_number=_PR_NUMBER,
        findings=findings,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["comments"] == []
    # The message text survives (escaped — a literal hyphen is itself a
    # Markdown special character neutralized by escape_markdown).
    assert "repo" in payload["body"] and "wide issue" in payload["body"]


# --- post_check_run: always neutral, escaped summary ------------------------


@respx.mock
async def test_post_check_run_posts_neutral_conclusion_with_count_summary(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    await _publisher(test_rsa_private_key_pem).post_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        finding_count=3,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"
    assert payload["head_sha"] == _HEAD_SHA
    assert "3" in payload["output"]["summary"]


@respx.mock
async def test_post_check_run_zero_findings_states_no_findings(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    await _publisher(test_rsa_private_key_pem).post_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        finding_count=0,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"
    assert "no findings" in payload["output"]["summary"].casefold()


# --- post_failure_check_run: failure conclusion ------------------------------


@respx.mock
async def test_post_failure_check_run_posts_failure_conclusion(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    await _publisher(test_rsa_private_key_pem).post_failure_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        failed_step_name="llm_review",
        error="boom",
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["conclusion"] == "failure"
    assert "llm" in payload["output"]["summary"] and "review" in payload["output"]["summary"]


@respx.mock
async def test_post_failure_check_run_escapes_error_text(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    await _publisher(test_rsa_private_key_pem).post_failure_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        failed_step_name="llm_review",
        error="<img src=x onerror=alert(1)>",
    )

    sent_body = route.calls.last.request.content.decode()
    assert "<img" not in sent_body


# --- Shared retry helper reuse + non-idempotent-write retry scoping --------


@respx.mock
async def test_post_calls_go_through_shared_retry_helper(test_rsa_private_key_pem: str) -> None:
    """Asserts the helper is invoked (a 429 gets transparently retried),
    not re-testing the retry logic itself — per Phase 4's own Tests note
    ("mirroring how Phase 5's TextRedactor is tested once ... and reused
    elsewhere, not re-verified per call site")."""
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.001"}),
        httpx.Response(201, json={"id": 1}),
    ]

    await _publisher(test_rsa_private_key_pem).post_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        finding_count=1,
    )

    assert route.call_count == 2


@respx.mock
async def test_post_review_bare_5xx_fails_fatally_on_first_response_no_retry(
    test_rsa_private_key_pem: str,
) -> None:
    """Proves the non-idempotent-write retry scoping specific to this call
    site: a bare 5xx (or ambiguous timeout) is never retried, since GitHub
    may already have processed the write — per Phase 4's own
    idempotency-scoping bullet."""
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(GitHubPostingUnreachableError):
        await _publisher(test_rsa_private_key_pem).post_review(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            pr_number=_PR_NUMBER,
            findings=[_finding()],
        )

    assert route.call_count == 1


@respx.mock
async def test_post_check_run_rate_limited_response_is_retried_and_eventually_succeeds(
    test_rsa_private_key_pem: str,
) -> None:
    """The companion positive case: 429/secondary-rate-limit *is* retried,
    proving posting.py correctly retries the cases that are safe, not only
    correctly declining the ones that aren't."""
    _mock_token_exchange()
    route = respx.post(f"{_BASE}/repos/{_OWNER_REPO}/check-runs")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.001"}),
        httpx.Response(201, json={"id": 1}),
    ]

    await _publisher(test_rsa_private_key_pem).post_check_run(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        head_sha=_HEAD_SHA,
        finding_count=0,
    )

    assert route.call_count == 2
