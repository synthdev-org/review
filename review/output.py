"""review.output — the findings-merge-and-output macro pipeline step (US6,
US7, US8), per the plan's Phase 7 section: dedupes/merges static-analysis
and LLM-sourced findings (``findings.merge.merge_findings``), then posts
them as one batched PR review plus a check-run via the GitHub Adapter — or,
on a fatal upstream failure, posts a failure-conclusion check-run instead,
per this step's own "terminal step" role (``review.pipeline.StepRegistry``'s
own docstring covers the mechanism; this module is its one Phase 1
consumer).

**Two shapes of ``input``, reconciling with the ``Step`` Protocol's own
invariant rather than a silent exception to it** (``review/step.py``): this
step's ``run()`` accepts ``FindingsOutputInput | StepFailureInfo`` — still
one step-specific pydantic model per call, just a union of two shapes for
the one step in this framework ever invoked on both a success and a failure
path. ``isinstance(input, StepFailureInfo)`` distinguishes them at runtime;
every other step's own ``input`` stays the single concrete model it always
was.

**Protocol boundary:** ``FindingsPublisher`` (defined here, the consumer,
per architecture.md — this Protocol has exactly one real consumer, this
module, so it follows the ordinary "defined at the consumer" rule
unchanged, unlike ``ingestion.diff.DiffFetcher``/``findings.redact.TextRedactor``,
which needed ``docs/adr/0006-protocol-defined-at-data-model.md``'s
exemption for their own multi-consumer case). Implemented by
``adapter/github/posting.py``; this module never imports that adapter
directly, only the Protocol — the concrete client is wired in only at the
composition root (``apps/worker/main.py``), mirroring every other
adapter/domain pairing this plan builds.

**Dry-run.** ``context.dry_run`` (threaded through every step's shared
``StepContext``, per ``review/step.py``'s own docstring) is honored on
*both* paths — the happy path and the failure path alike, not only the
former: an operator validating a fresh deployment via dry-run is likeliest
to hit exactly the failure path (a misconfiguration causing an early step
to fail), so leaving that path un-dry-run-aware would make a live GitHub
write happen during the one validation scenario dry-run exists to cover.
When set, this step runs its own merge logic (or, on the failure path,
nothing further) and logs the would-be comment/check-run payload at
``info`` instead of calling ``adapter/github/posting.py`` — the same
redacted ``Finding`` objects a real post would use (Phase 5/6's
``TextRedactor`` pass already ran on every finding before this step ever
sees them), not a second, unredacted view of them.

**Happy-path partial-failure, specified rather than left as a gap:**
``post_review(...)`` is called before ``post_check_run(...)`` (the
higher-value artifact — the inline findings a reviewer actually reads —
posted first). If the first call succeeds and the second then fails, this
is logged as a distinct, ``error``-level partial-post defect (not the
``warning`` a routine transient GitHub failure gets) naming the PR number
and which call succeeded/failed, before the exception is re-raised
(propagating to the macro-step-fatal path, which the outer registry's own
generic failure log already covers) — real, customer-visible output (the
PR review) is already permanently live on the customer's repo with no
check-run to accompany it, and that gap needs to be loud for an operator to
manually reconcile, not silently indistinguishable from any other step
failure. A zero-finding review skips ``post_review(...)`` entirely (per
US7 — "zero inline comments" is not "a review submission with zero
comments"), so a ``post_check_run(...)`` failure in that case is an
ordinary, non-partial fatal failure, not this scenario.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from findings.merge import POSTED_FINDINGS_CEILING, merge_findings
from findings.models import Finding
from review.pipeline import StepFailureInfo
from review.step import Step, StepContext

logger = logging.getLogger(__name__)


class FindingsPublisher(Protocol):
    """GitHub-posting capability this step depends on — per
    architecture.md, a narrow Protocol defined at its one consumer.
    Implemented by ``adapter/github/posting.py``'s ``GitHubFindingsPublisher``.
    """

    async def post_review(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        pr_number: int,
        findings: list[Finding],
    ) -> None:
        """Post one batched PR review carrying every inline comment in a
        single call, per both PRDs' NFR batching decision. Never called
        with an empty ``findings`` list (see this module's own docstring's
        "zero-finding review" note)."""
        ...

    async def post_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        finding_count: int,
    ) -> None:
        """Post a ``"neutral"``-conclusion check-run summarizing
        ``finding_count`` — always, per US7, whether that count is zero or
        not."""
        ...

    async def post_failure_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        failed_step_name: str,
        error: str,
    ) -> None:
        """Post a failure-conclusion check-run — this step's own failure
        path (US8), invoked as this registry's terminal step regardless of
        which upstream macro step actually failed."""
        ...


class FindingsOutputInput(BaseModel):
    """This step's own ``input`` on the happy path — mirrors
    ``review.llm_review.LLMReviewOutput``'s field shape without importing
    it, the same "Import boundary" pattern every downstream step in this
    plan already follows for its own upstream dependency (per US-F1;
    ``review/test_step_boundaries.py``).

    Also, deliberately, the shape ``review.pipeline.StepFailureInfo.seed_input``
    is validated against on the *failure* path (``_run_failure_path``
    below) — the raw job payload (``apps/webhook_api/webhook_routes.py``'s
    own ``_enqueue_payload``, mirrored exactly by ``review.prefilter.PrefilterInput``)
    already carries every field here except ``findings``, which is why that
    one field alone defaults to empty: a fatal failure before this step's
    normal dependency ever produced output has no findings to report, by
    definition (US8 — "posts no inline PR comments").
    """

    model_config = ConfigDict(frozen=True, from_attributes=True, arbitrary_types_allowed=True)

    findings: list[Finding] = Field(default_factory=list)
    org_id: str
    repo_id: str
    installation_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str


class FindingsOutputResult(BaseModel):
    """This step's own output — nothing downstream depends on it (this is
    the pipeline's terminal step), but every other step in this plan
    returns a typed model rather than ``None``, and this shape is what this
    module's own tests assert against."""

    model_config = ConfigDict(frozen=True)

    comment_count: int
    check_run_conclusion: Literal["neutral", "failure"]
    posted: bool
    """``False`` when ``context.dry_run`` suppressed the real GitHub
    call(s) — the merge/redaction logic still ran, only the write didn't."""


class FindingsOutputStep:
    """``review.step.Step`` implementation for US6/US7/US8 — see this
    module's own docstring for the full design. Registered as this plan's
    ``StepRegistry.terminal_step`` (``apps/worker/main.py``) so it also
    runs on a fatal upstream failure, per ``review.pipeline.StepRegistry``'s
    own "Terminal step" docstring note.
    """

    name = "output"

    def __init__(self, publisher: FindingsPublisher, *, depends_on: str = "llm_review") -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"llm_review"`` by default (the ordinary, both-steps-enabled case),
        or ``"static_analysis"`` when the composition root
        (``apps/worker/main.py``) determines ``llm_review`` is disabled.

        **Why this is a constructor parameter, not a fixed class attribute
        (a bug this plan's own review caught):** ``review.pipeline.StepRegistry``
        resolves each step's ``input`` from exactly its declared dependency's
        output (``_resolve_input``); a disabled step never produces one. With
        a fixed ``depends_on=("llm_review",)``, disabling ``llm_review`` — a
        configuration this phase's own "Rollback" section documents as a
        supported fallback to static-analysis-only output — left this step
        with no satisfiable dependency, and the registry's own generic
        "no defined behavior for that combination" ``RuntimeError`` propagated
        as an unhandled exception, bypassing the terminal-step failure-signal
        mechanism (Phase 7's own "always produce a GitHub-visible signal"
        goal) entirely. The composition root, which already knows
        ``StepEnablement.llm_review_enabled`` before it builds this registry,
        instead passes the one upstream step actually guaranteed to run —
        ``check_step_configuration_viable`` (``review/config.py``) already
        proves ``static_analysis`` can produce findings whenever
        ``llm_review`` is disabled, so this is never ambiguous.
        ``LLMReviewOutput`` and ``StaticAnalysisOutput`` share the same
        `from_attributes`-readable field shape `FindingsOutputInput` needs
        (both carry `findings`/`org_id`/`repo_id`/`installation_id`/
        `repo_full_name`/`pr_number`/`head_sha`), so no change to this
        step's own input handling is needed — only which step's output it
        reads.
        """
        self._publisher = publisher
        self.depends_on: tuple[str, ...] = (depends_on,)

    async def run(self, input: Any, context: StepContext) -> FindingsOutputResult:
        # Any (not a pydantic model) because this is the one step whose
        # input is a genuine union: the registry hands it either a normal
        # upstream step output (validated as FindingsOutputInput below) or a
        # StepFailureInfo when an earlier step failed fatally. The isinstance
        # check below narrows before any parsing happens.
        if isinstance(input, StepFailureInfo):
            return await self._run_failure_path(input, context)
        parsed = FindingsOutputInput.model_validate(input, from_attributes=True)
        return await self._run_happy_path(parsed, context)

    async def _run_happy_path(
        self, parsed: FindingsOutputInput, context: StepContext
    ) -> FindingsOutputResult:
        merge_result = merge_findings(parsed.findings)
        findings = merge_result.findings
        if merge_result.dropped_by_ceiling_count:
            logger.warning(
                "findings-merge: posted-findings ceiling exceeded; dropping excess findings",
                extra={
                    "job_id": context.job_id,
                    "ceiling": POSTED_FINDINGS_CEILING,
                    "dropped_count": merge_result.dropped_by_ceiling_count,
                },
            )

        if context.dry_run:
            logger.info(
                "dry-run: would post PR review and check-run",
                extra={
                    "job_id": context.job_id,
                    "pr_number": parsed.pr_number,
                    "comment_count": len(findings),
                    "check_run_conclusion": "neutral",
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                },
            )
            return FindingsOutputResult(
                comment_count=len(findings), check_run_conclusion="neutral", posted=False
            )

        if findings:
            await self._publisher.post_review(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                pr_number=parsed.pr_number,
                findings=findings,
            )

        try:
            await self._publisher.post_check_run(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                head_sha=parsed.head_sha,
                finding_count=len(findings),
            )
        except Exception as exc:
            if findings:
                logger.error(
                    "findings-output: PR review posted but check-run failed — partial "
                    "post, needs manual reconciliation",
                    extra={
                        "job_id": context.job_id,
                        "pr_number": parsed.pr_number,
                        "post_review": "succeeded",
                        "post_check_run": "failed",
                        "error": str(exc),
                    },
                )
            raise

        logger.info(
            "findings-output: posted",
            extra={
                "job_id": context.job_id,
                "pr_number": parsed.pr_number,
                "comment_count": len(findings),
                "check_run_conclusion": "neutral",
            },
        )
        return FindingsOutputResult(
            comment_count=len(findings), check_run_conclusion="neutral", posted=True
        )

    async def _run_failure_path(
        self, info: StepFailureInfo, context: StepContext
    ) -> FindingsOutputResult:
        parsed = FindingsOutputInput.model_validate(info.seed_input, from_attributes=True)

        if context.dry_run:
            logger.info(
                "dry-run: would post failure-conclusion check-run",
                extra={
                    "job_id": context.job_id,
                    "pr_number": parsed.pr_number,
                    "failed_step_name": info.failed_step_name,
                    "check_run_conclusion": "failure",
                },
            )
            return FindingsOutputResult(
                comment_count=0, check_run_conclusion="failure", posted=False
            )

        await self._publisher.post_failure_check_run(
            installation_id=parsed.installation_id,
            repo_full_name=parsed.repo_full_name,
            head_sha=parsed.head_sha,
            failed_step_name=info.failed_step_name,
            error=info.error,
        )

        logger.info(
            "findings-output: posted failure-conclusion check-run",
            extra={
                "job_id": context.job_id,
                "pr_number": parsed.pr_number,
                "failed_step_name": info.failed_step_name,
            },
        )
        return FindingsOutputResult(comment_count=0, check_run_conclusion="failure", posted=True)


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[Step[Any, FindingsOutputResult]] = FindingsOutputStep
