"""Integration tests for review.output.FindingsOutputStep (US6, US7, US8),
per the plan's Phase 7 Tests section and docs/rules/testing.md's "GitHub
adapter against a recorded cassette (respx for httpx)" convention.

Exercises the real ``adapter.github.posting.GitHubFindingsPublisher``
against a stub GitHub API (``respx``) — not a fake ``FindingsPublisher`` —
wired into the real ``FindingsOutputStep``, itself run through a real
``review.pipeline.StepRegistry`` with ``terminal_step="output"`` so the
failure-path mechanism (``review.pipeline.StepRegistry``'s own docstring)
is exercised the same way ``apps/worker/main.py`` actually wires it, not
called directly.

The concrete ``TextRedactor``/analyzer-adapter tests, and the full,
fully-wired four-macro-step registry (US8/US9 against every real step, not
just this one) live in ``review/test_review_job.py`` per the PRD's own
Testing table — this file's own scope is this one step's own behavior
(merge, dry-run, the terminal-step wiring, the happy-path partial-failure
case) against a real posting client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.posting import GitHubFindingsPublisher
from findings.models import Finding
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.step import StepContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def test_rsa_private_key_pem() -> str:
    """A real (module-generated, never committed) RSA private key — genuinely
    usable by `adapter.github.auth.sign_app_jwt`'s real RS256 signing path.
    Defined locally rather than imported from `adapter/github/conftest.py`
    (a sibling package's fixture, out of this file's own pytest
    fixture-discovery scope) — the same self-contained-fixture convention
    `apps/worker/conftest.py` already establishes for the same key shape."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


@pytest.fixture(autouse=True)
def _reenable_output_logger() -> None:
    """See `adapter/github/test_http.py`'s own identical fixture docstring:
    Alembic's `env.py` (triggered by any `migrated_engine`-using test
    elsewhere in the same `pytest -m integration` process) disables every
    already-created logger not listed in `alembic.ini`, including this
    module's own `review.output` logger — re-enabled here, autouse, so
    every `caplog`-asserting case below stays deterministic regardless of
    test order.
    """
    logging.getLogger("review.output").disabled = False


_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_BASE = "https://api.github.com"
_REVIEWS_URL = f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/reviews"
_CHECK_RUNS_URL = f"{_BASE}/repos/{_OWNER_REPO}/check-runs"


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _publisher(test_rsa_private_key_pem: str) -> GitHubFindingsPublisher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubFindingsPublisher(token_provider=provider)


@dataclass
class _FakeUpstreamOutput:
    """Stands in for `review.llm_review.LLMReviewOutput` — this step's own
    real `input`, validated structurally (`from_attributes=True`) rather
    than requiring the real upstream step's own module import."""

    findings: list[Finding]
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = _INSTALLATION_ID
    repo_full_name: str = _OWNER_REPO
    pr_number: int = _PR_NUMBER
    head_sha: str = _HEAD_SHA


@dataclass
class _FakeUpstreamStep:
    """A fake `llm_review` step — this file's own concern is
    `FindingsOutputStep`'s behavior, not re-proving llm-review's own (that's
    `review/test_llm_review_step.py`'s job)."""

    output: _FakeUpstreamOutput | None = None
    error: Exception | None = None
    name: str = "llm_review"
    depends_on: tuple[str, ...] = ()
    calls: list[object] = field(default_factory=list)

    async def run(self, input: object, context: StepContext) -> _FakeUpstreamOutput:
        self.calls.append(input)
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output


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


def _registry(
    upstream: _FakeUpstreamStep,
    publisher: GitHubFindingsPublisher,
    *,
    depends_on: str = "llm_review",
) -> StepRegistry:
    step = FindingsOutputStep(publisher, depends_on=depends_on)
    return StepRegistry(
        [upstream, step],
        enabled={upstream.name: True, "output": True},
        timeouts={upstream.name: 5, "output": 5},
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )


def _context(*, dry_run: bool = False) -> StepContext:
    return StepContext(job_id="job-1", dry_run=dry_run)


_SEED_INPUT = {
    "org_id": "org-1",
    "repo_id": "repo-1",
    "installation_id": _INSTALLATION_ID,
    "repo_full_name": _OWNER_REPO,
    "pr_number": _PR_NUMBER,
    "head_sha": _HEAD_SHA,
}


# --- US6: combined findings posted as one batched review -------------------


@respx.mock
async def test_combined_findings_posted_as_one_batched_review(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    findings = [
        _finding(file="app.py", line=1, message="static finding", source="semgrep"),
        _finding(file="app.py", line=2, message="llm finding", source="llm"),
    ]
    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=findings))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    outcomes = await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 1
    assert outcomes["output"].status == "success"
    assert outcomes["output"].output.comment_count == 2


# --- US7: check-run always posted, zero or non-zero findings ---------------


@respx.mock
async def test_zero_findings_posts_no_findings_neutral_check_run_and_no_review(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    outcomes = await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 0
    assert check_run_route.call_count == 1
    assert outcomes["output"].output.check_run_conclusion == "neutral"


@respx.mock
async def test_non_zero_findings_posts_neutral_check_run_with_count(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[_finding()]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    await registry.run(_SEED_INPUT, _context())

    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "neutral"
    assert "1" in payload["output"]["summary"]


# --- US8: fatal upstream failure -> failure-conclusion check-run -----------


@respx.mock
async def test_fatal_upstream_failure_posts_failure_check_run_and_zero_comments(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(error=RuntimeError("llm gateway timed out"))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    with pytest.raises(PipelineFailedError):
        await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 0
    assert check_run_route.call_count == 1

    payload = json.loads(check_run_route.calls.last.request.content)
    assert payload["conclusion"] == "failure"


# --- Dry-run: both branches make zero live calls ----------------------------


@respx.mock
async def test_dry_run_happy_path_makes_zero_calls_and_logs_would_be_payload(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[_finding()]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    with caplog.at_level("INFO", logger="review.output"):
        outcomes = await registry.run(_SEED_INPUT, _context(dry_run=True))

    assert reviews_route.call_count == 0
    assert check_run_route.call_count == 0
    assert outcomes["output"].output.posted is False
    assert any("dry-run" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_dry_run_failure_path_makes_zero_calls_and_logs_would_be_failure_payload(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(error=RuntimeError("llm gateway timed out"))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    with (
        caplog.at_level("INFO", logger="review.output"),
        pytest.raises(PipelineFailedError),
    ):
        await registry.run(_SEED_INPUT, _context(dry_run=True))

    assert reviews_route.call_count == 0
    assert check_run_route.call_count == 0
    assert any("dry-run" in r.getMessage() and "failure" in r.getMessage() for r in caplog.records)


# --- Happy-path partial failure: post_review succeeds, post_check_run fails


@respx.mock
async def test_partial_post_when_review_succeeds_but_check_run_fails_is_logged_distinctly(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(503))

    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[_finding()]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    with caplog.at_level("ERROR", logger="review.output"), pytest.raises(PipelineFailedError):
        await registry.run(_SEED_INPUT, _context())

    # post_review reached the stub server exactly once (not retried, not
    # rolled back) — the "already-live, not compensated" behavior the
    # module docstring describes.
    assert reviews_route.call_count == 1
    assert check_run_route.call_count == 1
    partial_post_logs = [r for r in caplog.records if "partial" in r.getMessage()]
    assert len(partial_post_logs) == 1
    assert partial_post_logs[0].__dict__["pr_number"] == _PR_NUMBER
    assert partial_post_logs[0].__dict__["post_review"] == "succeeded"
    assert partial_post_logs[0].__dict__["post_check_run"] == "failed"


# --- Rollback: depends_on is configurable, not hardcoded to llm_review ------


@respx.mock
async def test_depends_on_static_analysis_reads_that_step_own_output_when_configured(
    test_rsa_private_key_pem: str,
) -> None:
    """Phase 6's own "Rollback" claim — disabling llm_review falls back to
    static-analysis-only output — depends on `FindingsOutputStep.depends_on`
    being settable to `"static_analysis"` rather than a fixed
    `("llm_review",)`. This is the single-step-tier proof that a fake
    `static_analysis`-named upstream step's own output flows through
    unchanged; `review/test_review_job.py`'s own equivalent case proves the
    same thing against the real, fully-wired four-macro-step registry with a
    llm-review step that would fail the job if it were invoked at all."""
    _mock_token_exchange()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    check_run_route = respx.post(_CHECK_RUNS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    upstream = _FakeUpstreamStep(
        name="static_analysis", output=_FakeUpstreamOutput(findings=[_finding()])
    )
    registry = _registry(
        upstream, _publisher(test_rsa_private_key_pem), depends_on="static_analysis"
    )

    outcomes = await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 1
    assert check_run_route.call_count == 1
    assert outcomes["output"].status == "success"
    assert outcomes["output"].output.comment_count == 1
