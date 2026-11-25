"""Shared test fixtures for adapter.github.

``test_rsa_private_key_pem`` is a real (freshly generated per test session,
never a committed static blob) RSA private key — genuinely usable by
``adapter.github.auth.sign_app_jwt``'s real RS256 signing path, needed by
every Phase 4 test exercising a real JWT-sign-and-exchange call
(``test_auth.py``, ``test_diff.py``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.posting import GitHubFindingsPublisher
from findings.models import Finding
from review.output import FindingsOutputInput, FindingsOutputStep, ShowNitsResolver
from review.step import StepContext


@pytest.fixture(scope="session")
def test_rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


# --- Phase 3 integration support: the real FindingsOutputStep driving the real
# GitHubFindingsPublisher over respx (GitHub is stubbed, never called) --------

_TEST_OWNER_REPO = "acme/widgets"
_TEST_INSTALLATION_ID = 555
_TEST_BASE = "https://api.github.com"


class PostedToGitHub:
    """What the stubbed GitHub API received for one step run."""

    def __init__(self) -> None:
        self.review: dict[str, Any] | None = None
        self.check_run: dict[str, Any] | None = None


class _NoopRecorder:
    async def record_comment_provenance(self, **_: Any) -> None:
        return None

    async def record_signal(self, **_: Any) -> bool:
        raise AssertionError("not exercised")


class _FixedNits:
    def __init__(self, answer: bool) -> None:
        self._answer = answer

    async def show_nits(self, org_id: str) -> bool:
        return self._answer


RunOutputStep = Callable[..., Awaitable[PostedToGitHub]]


@pytest.fixture
def run_output_step(test_rsa_private_key_pem: str) -> RunOutputStep:
    """``await run(findings, show_nits=False, v2=True)`` runs the real output step
    and returns the review and check-run bodies GitHub would have received."""

    async def run(
        findings: list[Finding],
        *,
        show_nits: bool = False,
        v2: bool = True,
        resolver: ShowNitsResolver | None = None,
    ) -> PostedToGitHub:
        posted = PostedToGitHub()

        def capture(slot: str) -> Callable[[httpx.Request], httpx.Response]:
            def handler(request: httpx.Request) -> httpx.Response:
                setattr(posted, slot, json.loads(request.content))
                return httpx.Response(200, json={"id": 1})

            return handler

        base = f"{_TEST_BASE}/repos/{_TEST_OWNER_REPO}"
        with respx.mock(assert_all_called=False) as router:
            router.post(
                f"{_TEST_BASE}/app/installations/{_TEST_INSTALLATION_ID}/access_tokens"
            ).mock(
                return_value=httpx.Response(
                    201, json={"token": "ghs_test", "expires_at": "2999-01-01T00:00:00Z"}
                )
            )
            router.post(f"{base}/pulls/42/reviews").mock(side_effect=capture("review"))
            router.get(f"{base}/pulls/42/reviews/1/comments").mock(
                return_value=httpx.Response(200, json=[])
            )
            router.post(f"{base}/check-runs").mock(side_effect=capture("check_run"))
            publisher = GitHubFindingsPublisher(
                token_provider=GitHubInstallationTokenProvider(
                    app_id=1, private_key_pem=test_rsa_private_key_pem
                ),
                provenance_recorder=_NoopRecorder(),
            )
            step = FindingsOutputStep(
                publisher, show_nits_resolver=resolver or _FixedNits(show_nits)
            )
            await step.run(
                FindingsOutputInput(
                    findings=findings,
                    org_id="org-1",
                    repo_id="repo-1",
                    installation_id=_TEST_INSTALLATION_ID,
                    repo_full_name=_TEST_OWNER_REPO,
                    pr_number=42,
                    head_sha="a" * 40,
                ),
                StepContext(job_id="job-1", review_quality_v2=v2),
            )
        return posted

    return run
