"""Tests for adapter.github.posting — the concrete FindingsPublisher
implementation, per docs/rules/testing.md's "GitHub adapter against a
recorded cassette (respx for httpx)" convention and the plan's Phase 7
Tests section.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import httpx
import pytest
import respx

from adapter.github import posting as posting_module
from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import GitHubPostingUnreachableError
from adapter.github.posting import GitHubFindingsPublisher
from findings.models import Finding
from memory.models import PostedComment

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _reenable_posting_logger() -> None:
    """See `adapter/github/test_http.py`'s own identical fixture docstring:
    keeps every `caplog`-asserting case below (Phase 2's provenance-capture
    logging) deterministic regardless of test order."""
    logging.getLogger("adapter.github.posting").disabled = False


_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_REPO_ID = "11111111-1111-1111-1111-111111111111"
_REVIEW_ID = 99
_BASE = "https://api.github.com"
_REVIEWS_URL = f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
_REVIEW_COMMENTS_URL = f"{_REVIEWS_URL}/{_REVIEW_ID}/comments"


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _mock_reviews_post() -> respx.Route:
    """Mocks the create-review call with a fixed ``id`` (``_REVIEW_ID``) so
    every test in this file can predict the follow-up provenance-capture
    GET URL (``_REVIEW_COMMENTS_URL``) without reading the request back."""
    return respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": _REVIEW_ID}))


def _mock_review_comments(comments: list[dict[str, object]] | None = None) -> respx.Route:
    """Mocks Phase 2 (US-M11)'s follow-up ``GET .../reviews/{id}/comments``
    call — defaults to an empty list (correlates to nothing, records
    nothing), which is all every pre-Phase-2 test in this file needs to keep
    passing under respx's default strict (``assert_all_mocked``) mode."""
    return respx.get(_REVIEW_COMMENTS_URL).mock(
        return_value=httpx.Response(200, json=comments or [])
    )


class _FakeProvenanceRecorder:
    """A ``memory.store.MemorySignalRecorder`` fake recording every
    ``record_comment_provenance`` call it receives, for this file's own
    Phase 2 (US-M11) assertions — ``record_signal`` is Phase 3/4's own
    concern, never exercised by ``post_review``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str, tuple[PostedComment, ...]]] = []

    async def record_comment_provenance(
        self,
        *,
        repo_id: str,
        pr_number: int,
        commit_sha: str,
        comments: Sequence[PostedComment],
    ) -> None:
        self.calls.append((repo_id, pr_number, commit_sha, tuple(comments)))

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: str,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> bool:
        raise AssertionError("record_signal is not exercised by adapter.github.posting")


def _publisher(
    test_rsa_private_key_pem: str,
    *,
    max_retry_attempts: int = 3,
    provenance_recorder: _FakeProvenanceRecorder | None = None,
) -> GitHubFindingsPublisher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubFindingsPublisher(
        token_provider=provider,
        provenance_recorder=provenance_recorder or _FakeProvenanceRecorder(),
        max_retry_attempts=max_retry_attempts,
    )


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
    route = _mock_reviews_post()
    _mock_review_comments()

    findings = [
        _finding(file="app.py", line=10, message="secret A"),
        _finding(file="lib.py", line=20, message="secret B", source="semgrep"),
    ]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
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
    route = _mock_reviews_post()
    _mock_review_comments()

    findings = [_finding(message="click [here](https://evil.example) @someone")]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
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
    route = _mock_reviews_post()
    _mock_review_comments()

    findings = [
        _finding(
            message="some_message",
            citation_markdown="**Context:**\n- `src/caller.ts:10-12` (`caller`)",
        )
    ]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
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
    # No `_mock_review_comments()` here, deliberately: a file-level-only
    # review has zero line findings, so `post_review` never issues the
    # follow-up provenance-capture GET at all (see
    # `test_post_review_zero_line_findings_never_calls_provenance_recorder`
    # below) — an unmocked route respx would reject if this test's own
    # code path were wrong.

    findings = [_finding(line=None, message="repo-wide issue")]

    await _publisher(test_rsa_private_key_pem).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
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
            repo_id=_REPO_ID,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
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


# --- Provenance persistence (Phase 2, US-M11) -------------------------------


@respx.mock
async def test_post_review_persists_provenance_for_correlated_comments(
    test_rsa_private_key_pem: str,
) -> None:
    """US-M11: a successful `post_review` call with line-attributed findings
    issues the follow-up comments-GET and calls `record_comment_provenance`
    once with every correctly-correlated `PostedComment`."""
    _mock_token_exchange()
    _mock_reviews_post()

    findings = [
        _finding(file="app.py", line=10, message="secret A", rule_id="rule-a"),
        _finding(file="lib.py", line=20, message="secret B", rule_id="rule-b", source="semgrep"),
    ]
    comments_response = [
        {
            "id": 1001,
            "path": "app.py",
            "line": 10,
            "body": posting_module._finding_body(findings[0]),
        },
        {
            "id": 1002,
            "path": "lib.py",
            "line": 20,
            "body": posting_module._finding_body(findings[1]),
        },
    ]
    _mock_review_comments(comments_response)

    recorder = _FakeProvenanceRecorder()
    await _publisher(test_rsa_private_key_pem, provenance_recorder=recorder).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        findings=findings,
    )

    assert len(recorder.calls) == 1
    repo_id, pr_number, commit_sha, comments = recorder.calls[0]
    assert repo_id == _REPO_ID
    assert pr_number == _PR_NUMBER
    assert commit_sha == _HEAD_SHA
    assert {c.comment_id for c in comments} == {1001, 1002}
    by_id = {c.comment_id: c for c in comments}
    assert by_id[1001].file == "app.py"
    assert by_id[1001].line == 10
    assert by_id[1001].rule_id == "rule-a"
    assert by_id[1001].message == "secret A"
    assert by_id[1002].file == "lib.py"
    assert by_id[1002].rule_id == "rule-b"


@respx.mock
async def test_post_review_unmatched_comment_is_skipped_logged_and_counted(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A comment GitHub returns with no matching `(path, line, body)` triple
    is skipped and logged at `warning`, counted in the aggregate summary
    line, and never passed to `record_comment_provenance` — never raised,
    per this module's own "provenance capture is a secondary write-path
    concern" docstring note."""
    _mock_token_exchange()
    _mock_reviews_post()
    _mock_review_comments(
        [{"id": 1001, "path": "app.py", "line": 10, "body": "not what was submitted"}]
    )

    recorder = _FakeProvenanceRecorder()
    with caplog.at_level("WARNING", logger="adapter.github.posting"):
        await _publisher(test_rsa_private_key_pem, provenance_recorder=recorder).post_review(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            repo_id=_REPO_ID,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
            findings=[_finding(file="app.py", line=10, message="secret A")],
        )

    assert recorder.calls == []
    unmatched_logs = [r for r in caplog.records if "did not correlate" in r.getMessage()]
    assert len(unmatched_logs) == 1
    assert unmatched_logs[0].__dict__["comment_id"] == 1001
    summary_logs = [r for r in caplog.records if "correlation summary" in r.getMessage()]
    assert len(summary_logs) == 1
    assert summary_logs[0].levelname == "WARNING"
    assert summary_logs[0].__dict__["matched_count"] == 0
    assert summary_logs[0].__dict__["unmatched_count"] == 1


@respx.mock
async def test_post_review_zero_line_findings_never_calls_provenance_recorder(
    test_rsa_private_key_pem: str,
) -> None:
    """A file-level-only review (no line-attributed findings at all) never
    issues the follow-up comments-GET and never calls
    `record_comment_provenance` — nothing could be correlated against
    GitHub's line-comment API in the first place. `_mock_review_comments()`
    is deliberately not called here: if `post_review` issued the GET call
    anyway, respx's default strict mode would fail this test on the
    unmocked request."""
    _mock_token_exchange()
    _mock_reviews_post()

    recorder = _FakeProvenanceRecorder()
    await _publisher(test_rsa_private_key_pem, provenance_recorder=recorder).post_review(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        repo_id=_REPO_ID,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        findings=[_finding(line=None, message="repo-wide issue")],
    )

    assert recorder.calls == []


@respx.mock
async def test_post_review_all_matched_logs_info_level_summary(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The aggregate summary line logs at `info`, not `warning`, when every
    returned comment correlated successfully (`unmatched_count == 0`)."""
    _mock_token_exchange()
    _mock_reviews_post()
    finding = _finding(file="app.py", line=10, message="secret A")
    _mock_review_comments(
        [{"id": 1001, "path": "app.py", "line": 10, "body": posting_module._finding_body(finding)}]
    )

    with caplog.at_level("INFO", logger="adapter.github.posting"):
        await _publisher(test_rsa_private_key_pem).post_review(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            repo_id=_REPO_ID,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
            findings=[finding],
        )

    summary_logs = [r for r in caplog.records if "correlation summary" in r.getMessage()]
    assert len(summary_logs) == 1
    assert summary_logs[0].levelname == "INFO"
    assert summary_logs[0].__dict__["matched_count"] == 1
    assert summary_logs[0].__dict__["unmatched_count"] == 0


@respx.mock
async def test_post_review_malformed_comments_get_body_does_not_raise(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A comments-GET response that isn't valid JSON (an unexpected shape
    change on GitHub's side) must not propagate out of `post_review` — the
    review post itself already succeeded and this module's own docstring
    promises provenance capture "never raises". Regression test for a gap
    the Phase 2 review found: only the `request_with_retry` call itself was
    guarded, not the `.json()` parse of its response body."""
    _mock_token_exchange()
    _mock_reviews_post()
    respx.get(_REVIEW_COMMENTS_URL).mock(return_value=httpx.Response(200, content=b"not json"))

    recorder = _FakeProvenanceRecorder()
    with caplog.at_level("WARNING", logger="adapter.github.posting"):
        await _publisher(test_rsa_private_key_pem, provenance_recorder=recorder).post_review(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            repo_id=_REPO_ID,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
            findings=[_finding(file="app.py", line=10, message="secret A")],
        )

    assert recorder.calls == []
    failure_logs = [r for r in caplog.records if "failed unexpectedly" in r.getMessage()]
    assert len(failure_logs) == 1
    assert failure_logs[0].__dict__["pr_number"] == _PR_NUMBER


@respx.mock
async def test_post_review_comment_missing_id_is_skipped_not_raised(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A returned comment that otherwise correlates but is missing its own
    `id` field must be treated as unmatched (logged, counted), not crash on
    a `KeyError` — regression test for the same "never raises" gap as
    above, on the correlation loop's own `raw_comment["id"]` access."""
    _mock_token_exchange()
    _mock_reviews_post()
    finding = _finding(file="app.py", line=10, message="secret A")
    _mock_review_comments(
        [{"path": "app.py", "line": 10, "body": posting_module._finding_body(finding)}]
    )

    recorder = _FakeProvenanceRecorder()
    with caplog.at_level("WARNING", logger="adapter.github.posting"):
        await _publisher(test_rsa_private_key_pem, provenance_recorder=recorder).post_review(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            repo_id=_REPO_ID,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
            findings=[finding],
        )

    assert recorder.calls == []
    unmatched_logs = [r for r in caplog.records if "did not correlate" in r.getMessage()]
    assert len(unmatched_logs) == 1
    assert unmatched_logs[0].__dict__["comment_id"] is None
