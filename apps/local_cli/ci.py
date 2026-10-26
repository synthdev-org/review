"""GitHub Actions wiring for the local CLI (plan Phase 5; US-C1 to US-C3).

Composition-root glue, so it lives beside ``main.py``: it reads what the runner provides
(``LocalSettings`` holds the ``GITHUB_*`` values, parsed once), validates the pull-request event
file with pydantic, and wires the findings publishers. Findings are always printed first (the job
log is where they must survive a GitHub failure), then handed to GitHub.

Only the ``pull_request`` event is accepted. The example workflow never uses
``pull_request_target`` (docs/reference/ci-github-actions.md): a fork's pull request must not get
secrets while the job reads files the fork controls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field, SecretStr, ValidationError

from adapter.github.ci_review import GitHubCiReviewPublisher
from adapter.github.token_provider import StaticTokenProvider
from adapter.local.terminal import TerminalFindingsPublisher
from apps.local_cli.settings import LocalSettings
from findings.models import SEVERITY_RANK, Finding, Severity

# A pull_request event payload is tens of kilobytes; the cap only bounds a hostile file.
_MAX_EVENT_BYTES = 5_000_000
_REPOSITORY_RE = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")


class CiConfigurationError(Exception):
    """The job is a GitHub Actions run that cannot post a review; the message says what to set."""


class _Head(BaseModel):
    sha: str = Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")


class _PullRequest(BaseModel):
    number: int = Field(gt=0)
    head: _Head


class _Event(BaseModel):
    pull_request: _PullRequest | None = None


class CiContext(BaseModel):
    repo_full_name: str
    pr_number: int
    # The pull request's own head: its file lines are the ones GitHub anchors comments to, whereas
    # the checkout is the merge commit, whose line numbers shift when the base has moved.
    head_sha: str
    token: SecretStr


def load_ci_context(settings: LocalSettings) -> CiContext | None:
    """``None`` outside GitHub Actions. Inside it, everything needed to post, or
    ``CiConfigurationError`` naming what is missing."""
    if not settings.github_actions:
        return None
    # pull_request_target also carries a ``pull_request`` object but runs with secrets against a
    # fork's files: refuse it (and any other event) by name, not by payload shape.
    if settings.github_event_name != "pull_request":
        raise CiConfigurationError(
            f"the workflow event is {settings.github_event_name or 'unknown'!r}, not "
            "'pull_request'; run this job on pull_request only (never pull_request_target)"
        )
    if settings.github_token is None or not settings.github_token.get_secret_value():
        raise CiConfigurationError(
            "running in GitHub Actions but GITHUB_TOKEN is not set. Pass it to the step: "
            "env: GITHUB_TOKEN: ${{ github.token }}"
        )
    repository = settings.github_repository or ""
    if not _REPOSITORY_RE.fullmatch(repository):
        raise CiConfigurationError("GITHUB_REPOSITORY is missing or is not owner/repo")
    if settings.github_api_url and not settings.github_api_url.startswith("https://"):
        raise CiConfigurationError("GITHUB_API_URL must be an https:// URL")
    event_path = settings.github_event_path
    if event_path is None:
        raise CiConfigurationError("GITHUB_EVENT_PATH is not set")
    try:
        if event_path.stat().st_size > _MAX_EVENT_BYTES:
            raise CiConfigurationError("the GitHub event file is too large")
        event = _Event.model_validate(json.loads(event_path.read_bytes()))
    except (OSError, ValueError, ValidationError) as exc:
        raise CiConfigurationError(
            f"the GitHub event file could not be read ({type(exc).__name__})"
        ) from exc
    if event.pull_request is None:
        raise CiConfigurationError(
            "the workflow event is not a pull_request; run this job on pull_request only"
        )
    return CiContext(
        repo_full_name=repository,
        pr_number=event.pull_request.number,
        head_sha=event.pull_request.head.sha,
        token=settings.github_token,
    )


class CiFindingsPublisher:
    """Prints every finding to the job log, then posts it to the pull request."""

    def __init__(
        self, terminal: TerminalFindingsPublisher, github: GitHubCiReviewPublisher
    ) -> None:
        self._terminal = terminal
        self.github = github

    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        findings: list[Finding],
    ) -> None:
        await self._terminal.post_review(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            pr_number=pr_number,
            head_sha=head_sha,
            findings=findings,
        )
        await self.github.post_review(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            pr_number=pr_number,
            head_sha=head_sha,
            findings=findings,
        )

    async def post_check_run(
        self, *, installation_id: int, repo_full_name: str, head_sha: str, finding_count: int
    ) -> None:
        await self._terminal.post_check_run(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            finding_count=finding_count,
        )
        await self.github.post_check_run(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            finding_count=finding_count,
        )

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        return None


def build_ci_publisher(
    settings: LocalSettings,
    ci: CiContext,
    terminal: TerminalFindingsPublisher,
    suppressed_lines: Callable[[], Sequence[str]] | None = None,
) -> CiFindingsPublisher:
    return CiFindingsPublisher(
        terminal,
        GitHubCiReviewPublisher(
            token_provider=StaticTokenProvider(ci.token),
            repo_full_name=ci.repo_full_name,
            pr_number=ci.pr_number,
            bot_login=settings.ci_bot_login,
            head_sha=ci.head_sha,
            timeout=settings.github_timeout_seconds,
            max_retry_attempts=settings.github_max_retry_attempts,
            suppressed_lines=suppressed_lines,
            github_api_base_url=settings.github_api_url or "https://api.github.com",
        ),
    )


def gate_count(threshold: Severity | None, findings: list[Finding]) -> int:
    """How many findings are at or above ``threshold`` (US-C2); 0 when no gate is configured."""
    if threshold is None:
        return 0
    return sum(1 for f in findings if SEVERITY_RANK[f.severity] <= SEVERITY_RANK[threshold])


__all__ = [
    "CiConfigurationError",
    "CiContext",
    "CiFindingsPublisher",
    "build_ci_publisher",
    "gate_count",
    "load_ci_context",
]
