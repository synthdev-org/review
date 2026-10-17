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
from memory.models import PostedComment
from review.context_bundle import (
    ContextBundle,
    ContextBundleItem,
    ContextItemKind,
    render_product_knowledge_section,
)
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.product_knowledge import ProductKnowledgeItem, ProductKnowledgeSection
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
_REVIEW_COMMENTS_URL = f"{_REVIEWS_URL}/1/comments"
_CHECK_RUNS_URL = f"{_BASE}/repos/{_OWNER_REPO}/check-runs"


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _mock_review_comments(comments: list[dict[str, object]] | None = None) -> None:
    """Mocks Phase 2 (US-M11)'s follow-up ``GET .../reviews/{id}/comments``
    call, issued by `GitHubFindingsPublisher.post_review` whenever a review
    with at least one line-attributed finding is posted (mocked review id is
    always `1` here, matching every `reviews_route`'s own mocked response
    body). Every real test in this file's own scope is
    `adapter/github/posting.py`'s posting/merge/dry-run/citation behavior,
    not this Phase 2 correlation logic itself (that's
    `adapter/github/test_posting.py`'s job per the plan's Phase 2 Tests
    section) — defaults to an empty list, which correlates to nothing and
    never calls the provenance recorder, so this mock never has an opinion
    on this file's own assertions.
    """
    respx.get(_REVIEW_COMMENTS_URL).mock(return_value=httpx.Response(200, json=comments or []))


class _FakeProvenanceRecorder:
    """A no-op `memory.store.MemorySignalRecorder` fake — this file's own
    scope is `FindingsOutputStep`/`GitHubFindingsPublisher`'s
    posting/merge/dry-run/citation behavior, not Phase 2's provenance
    persistence itself (see `adapter/github/test_posting.py`)."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, int, str, tuple[PostedComment, ...]]] = []

    async def record_comment_provenance(
        self, *, repo_id: str, pr_number: int, commit_sha: str, comments: object
    ) -> None:
        self.recorded.append((repo_id, pr_number, commit_sha, tuple(comments)))  # type: ignore[arg-type]

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: str,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> bool:
        raise AssertionError("record_signal is not exercised by this test file")


def _publisher(test_rsa_private_key_pem: str) -> GitHubFindingsPublisher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubFindingsPublisher(
        token_provider=provider, provenance_recorder=_FakeProvenanceRecorder()
    )


@dataclass
class _FakeUpstreamOutput:
    """Stands in for `review.verification.VerificationOutput` — this step's own
    real `input`, validated structurally (`from_attributes=True`) rather
    than requiring the real upstream step's own module import."""

    findings: list[Finding]
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = _INSTALLATION_ID
    repo_full_name: str = _OWNER_REPO
    pr_number: int = _PR_NUMBER
    head_sha: str = _HEAD_SHA
    # Added by docs/plans/plan-deliver-context-builder.md's Phase 6 section
    # (US6) — defaults to `None`, matching `FindingsOutputInput`'s own
    # default, so every existing test in this file that doesn't set it sees
    # byte-for-byte the same posted output as before this phase.
    context_bundle: ContextBundle | None = None


@dataclass
class _FakeUpstreamStep:
    """A fake `verification` step — this file's own concern is
    `FindingsOutputStep`'s behavior, not re-proving verification's own (that's
    `review/test_verification_step.py`'s job)."""

    output: _FakeUpstreamOutput | None = None
    error: Exception | None = None
    name: str = "verification"
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
    depends_on: str = "verification",
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
    _mock_review_comments()
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
    _mock_review_comments()
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
async def test_dry_run_happy_path_logs_the_citation_appended_message(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Phase 6 (US6)'s citation `model_copy` step must run *before* the
    dry-run early return, not only on the real-post path — this module's
    own docstring requires dry-run output to reflect "the same... objects a
    real post would use". Proves the dry-run log's own `findings` payload
    already has `citation_markdown` set (not `None`) — the specific
    regression risk the plan (round eight) named by name. `message` itself
    stays the original, citation-less text: `citation_markdown` is a
    separate field, never appended into `message` (see
    `Finding.citation_markdown`'s own docstring)."""
    _mock_token_exchange()
    respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="do_something",
                content="function caller() { return doSomething(); }",
                line_range=(10, 12),
            ),
        )
    )
    finding = _finding(
        file="app.py", line=2, message="llm finding", source="llm", context_ref_ids=(0,)
    )
    upstream = _FakeUpstreamStep(
        output=_FakeUpstreamOutput(findings=[finding], context_bundle=context_bundle)
    )
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    with caplog.at_level("INFO", logger="review.output"):
        await registry.run(_SEED_INPUT, _context(dry_run=True))

    dry_run_records = [r for r in caplog.records if "dry-run" in r.getMessage()]
    assert dry_run_records
    logged_findings = dry_run_records[0].__dict__["findings"]
    assert len(logged_findings) == 1
    assert logged_findings[0]["citation_markdown"] is not None
    # `.` is escaped by `render_citation` -- this is a plain Python string
    # here (`model_dump(mode="json")` produces JSON-*compatible* Python
    # values, not JSON *text*), so a single escaped `.` is one literal
    # backslash, not a doubled one.
    assert "src/caller\\.ts:10-12" in logged_findings[0]["citation_markdown"]
    assert logged_findings[0]["message"] == "llm finding"


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
    _mock_review_comments()
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


# --- Phase 6 (US6): citation rendering --------------------------------------


@respx.mock
async def test_finding_with_a_real_citation_posts_formatted_not_escaped(
    test_rsa_private_key_pem: str,
) -> None:
    """US6: a finding whose `context_ref_ids` resolve against the step
    input's own `context_bundle` gets a platform-rendered citation set on
    its own `citation_markdown` field *before* posting, and
    `adapter/github/posting.py` appends that field to the posted comment
    body *without* re-running it through `escape_markdown` — regression
    test for a real bug where the citation used to be appended into
    `message` and then escaped a second time downstream, which backslash-
    escaped the citation's own Markdown template (the bold `**Context:**`
    header, the backtick code-span delimiters) into literal, unformatted
    text. Under that old code this test's `"**Context:**" in posted_body`
    and backtick assertions below would have failed (the posted body would
    instead have contained `\\*\\*Context:\\*\\*` and `\\`...\\``).

    The embedded symbol name (`do_something_now`, containing `_` — a real
    GFM special character) must still be individually escaped — proving
    `review/citation.py` itself now does that escaping, rather than relying
    on (and losing to) a later blanket pass over the combined string.
    """
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="do_something_now",
                content="function caller() { return doSomethingNow(); }",
                line_range=(10, 12),
            ),
        )
    )
    finding = _finding(
        file="app.py", line=2, message="llm finding", source="llm", context_ref_ids=(0,)
    )
    upstream = _FakeUpstreamStep(
        output=_FakeUpstreamOutput(findings=[finding], context_bundle=context_bundle)
    )
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 1
    payload = json.loads(reviews_route.calls.last.request.content)
    posted_body = json.dumps(payload)
    # The citation's own template survives unescaped: a literal bold header
    # and literal backticks, not `\*\*Context:\*\*` / `\`...\`` -- under the
    # old buggy code this literal substring would not appear at all (it
    # would be backslash-escaped instead), so this assertion alone pins the
    # regression.
    assert "**Context:**" in posted_body
    # The embedded file path/symbol name are still individually escaped
    # (`.` and `_` are both in `escape_markdown`'s escape set) -- inside the
    # still-literal backticks, not instead of them.
    assert "`src/caller\\\\.ts:10-12` (`do\\\\_something\\\\_now`)" in posted_body
    assert "src/caller.ts" not in posted_body
    assert "do_something_now" not in posted_body


@respx.mock
async def test_finding_with_no_context_ref_ids_posts_exactly_as_a_phase_1_finding_would(
    test_rsa_private_key_pem: str,
) -> None:
    """US6's second Given/When/Then: a finding whose generation drew on no
    context-bundle item renders exactly as a Phase 1 finding would — no
    citation section appended — even when the step input carries a
    non-empty `context_bundle` (proving the absence of `context_ref_ids` on
    the finding itself, not the absence of a bundle, is what gates
    citation rendering)."""
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="caller",
                content="function caller() { return helper(); }",
                line_range=(1, 1),
            ),
        )
    )
    finding = _finding(file="app.py", line=1, message="static finding", source="semgrep")
    upstream = _FakeUpstreamStep(
        output=_FakeUpstreamOutput(findings=[finding], context_bundle=context_bundle)
    )
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 1
    payload = json.loads(reviews_route.calls.last.request.content)
    posted_body = json.dumps(payload)
    assert "src/caller.ts" not in posted_body
    assert "**Context:**" not in posted_body


# --- Rollback: depends_on is configurable, not hardcoded to verification ---


@respx.mock
async def test_depends_on_static_analysis_reads_that_step_own_output_when_configured(
    test_rsa_private_key_pem: str,
) -> None:
    """Phase 6's own "Rollback" claim — disabling verification falls back to
    static-analysis-only output — depends on `FindingsOutputStep.depends_on`
    being settable to `"static_analysis"` rather than a fixed
    `("verification",)`. This is the single-step-tier proof that a fake
    `static_analysis`-named upstream step's own output flows through
    unchanged; `review/test_review_job.py`'s own equivalent case proves the
    same thing against the real, fully-wired five-macro-step registry with a
    verification step that would fail the job if it were invoked at all."""
    _mock_token_exchange()
    _mock_review_comments()
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


@respx.mock
async def test_us_pk3_finding_citing_provider_item_posts_product_knowledge_block(
    test_rsa_private_key_pem: str,
) -> None:
    """
    US-PK3/US-PK6: a provider-item finding gets the platform-rendered block.

    A finding whose ``context_ref_ids`` resolve to a provider item is posted
    with the platform-rendered, unescaped ``**Product knowledge (<display>):**``
    block — link, source type and host only, never the provider's snippet.
    """
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    context_bundle = ContextBundle(
        items=(
            ContextBundleItem(
                id=0,
                kind=ContextItemKind.CALLER,
                file_path="src/caller.ts",
                symbol_name="caller",
                content="function caller() {}",
                line_range=(10, 12),
            ),
        ),
        product_knowledge=ProductKnowledgeSection(
            label="Acme Wiki",
            items=(
                ProductKnowledgeItem(
                    id=1,
                    provider_item_id="doc-1",
                    title="Refund *policy*",
                    source_type="confluence",
                    url="https://wiki.example.com/refunds",
                    content="PROVIDER-SNIPPET-MUST-NOT-BE-ECHOED",
                ),
            ),
        ),
    )
    finding = _finding(
        file="app.py", line=2, message="llm finding", source="llm", context_ref_ids=(0, 1)
    )
    upstream = _FakeUpstreamStep(
        output=_FakeUpstreamOutput(findings=[finding], context_bundle=context_bundle)
    )
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))
    # The generator can only cite what the wire section tells it to cite.
    wire_section = render_product_knowledge_section(context_bundle)
    assert "context_ref_ids" in wire_section and '"id": 1' in wire_section

    await registry.run(_SEED_INPUT, _context())

    assert reviews_route.call_count == 1
    posted_body = json.dumps(json.loads(reviews_route.calls.last.request.content))
    assert "**Context:**" in posted_body
    assert "**Product knowledge (Acme Wiki):**" in posted_body
    assert "[Refund \\\\*policy\\\\*](https://wiki.example.com/refunds)" in posted_body
    assert "confluence" in posted_body
    assert "PROVIDER-SNIPPET-MUST-NOT-BE-ECHOED" not in posted_body


# --- Phase 10 / Phase 6: clone_detection findings (US-C11, US-C13) ----------


@respx.mock
async def test_us_c13_over_ceiling_job_still_posts_security_findings_and_a_clone_finding_inline(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    secret = _finding(file="s.py", line=1, severity="critical", message="leaked key")
    clone = _finding(
        file="dup.py",
        line=5,
        severity="low",
        message="Duplicates other.py:10-30",
        source="clone_detection",
    )
    # Clone arrives first among the 100 same-severity low findings, so only the
    # ceiling ordering (not input order) can drop it.
    lows = [
        _finding(file="l.py", line=i, severity="low", message=f"lint issue {i}", source="ruff")
        for i in range(100)
    ]
    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[clone, *lows, secret]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    outcomes = await registry.run(_SEED_INPUT, _context())

    assert outcomes["output"].status == "success"
    comments = json.loads(reviews_route.calls.last.request.content)["comments"]
    assert len(comments) == 100
    assert comments[0]["path"] == "s.py"
    assert all(c["path"] != "dup.py" for c in comments)


@respx.mock
async def test_us_c11_surviving_clone_finding_posts_as_an_ordinary_inline_comment(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    clone = _finding(
        file="dup.py",
        line=5,
        severity="low",
        message="Duplicates other.py",
        source="clone_detection",
    )
    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[clone]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    await registry.run(_SEED_INPUT, _context())

    comments = json.loads(reviews_route.calls.last.request.content)["comments"]
    assert [(c["path"], c["line"]) for c in comments] == [("dup.py", 5)]
    assert "Duplicates other" in comments[0]["body"]


@respx.mock
async def test_us_c11_clone_and_llm_finding_on_the_same_line_dedupe_under_the_existing_rule(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    _mock_review_comments()
    reviews_route = respx.post(_REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(_CHECK_RUNS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    llm = _finding(
        file="dup.py", line=5, severity="medium", message="duplicated logic block", source="llm"
    )
    clone = _finding(
        file="dup.py",
        line=5,
        severity="low",
        message="duplicated logic block!",
        source="clone_detection",
    )
    upstream = _FakeUpstreamStep(output=_FakeUpstreamOutput(findings=[llm, clone]))
    registry = _registry(upstream, _publisher(test_rsa_private_key_pem))

    outcomes = await registry.run(_SEED_INPUT, _context())

    assert outcomes["output"].output.comment_count == 1
    assert len(json.loads(reviews_route.calls.last.request.content)["comments"]) == 1
