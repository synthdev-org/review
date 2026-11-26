"""review.output — the findings-merge-and-output macro pipeline step (US6,
US7, US8): dedupes/merges static-analysis
and LLM-sourced findings (``findings.merge.merge_findings``), then posts
them as one batched PR review plus a check-run via the GitHub Adapter — or,
on a fatal upstream failure, posts a failure-conclusion check-run instead,
per this step's own "terminal step" role (``review.pipeline.StepRegistry``'s
own docstring covers the mechanism; this module is its one
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
adapter/domain pairing.

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
redacted ``Finding`` objects a real post would use (the
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
from collections import Counter
from typing import Any, Literal, Protocol

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from findings.merge import (
    POSTED_FINDINGS_CEILING,
    MergeResult,
    effective_tier,
    merge_findings,
)
from findings.models import Finding
from ingestion.diff import Diff, commentable_lines
from review._telemetry import category_label, suppress_telemetry_errors
from review.citation import render_citation
from review.context_bundle import ContextBundle
from review.pipeline import StepFailureInfo
from review.step import Step, StepContext

logger = logging.getLogger(__name__)

# Catalog names, verbatim (docs/specs/spec-observability.md's METRIC & SPAN
# CATALOG, US4) — recorded here, at `merge_findings`'s one call site, rather
# than inside `findings/merge.py` itself, which stays a pure computation per
# that module's own docstring/architecture.md's domain-package rule (no infra
# imports there). Meter name is the catalog's own literal
# ("review.findings"), not `__name__` ("review.output") — this module is
# where the metric happens to be recorded, not what it's scoped to.
meter = metrics.get_meter("review.findings")

_findings_count_total = meter.create_counter(
    "review.findings.count_total",
    description="Findings surviving merge, by severity and source.",
)
_findings_dropped_by_ceiling_total = meter.create_counter(
    "review.findings.dropped_by_ceiling_total",
    description="Findings dropped by the posted-findings ceiling after dedupe.",
)
# v2 layout instruments; closed-set labels, allow-listed in
# docs/specs/spec-observability.md.
_findings_posted_by_tier_total = meter.create_counter(
    "review.findings.posted_by_tier_total",
    description="Findings posted inline under the v2 layout, by tier.",
)
_findings_collapsed_total = meter.create_counter(
    "review.findings.collapsed_total",
    description="Findings placed in the collapsed summary or hidden, by reason.",
)
_findings_deduped_total = meter.create_counter(
    "review.findings.deduped_total",
    description="Findings folded into another by the within-job root-cause dedupe.",
)
_findings_dropped_by_ceiling_by_tier_total = meter.create_counter(
    "review.findings.dropped_by_ceiling_by_tier_total",
    description="Findings cut by the posted-findings ceiling under v2, by tier.",
)
_citation_findings_total = metrics.get_meter("review.citation").create_counter(
    "review.citation.findings_total",
    description="Findings carrying context_ref_ids, by whether any citation rendered.",
)


def _record_findings_metrics(merge_result: MergeResult) -> None:
    """Records US4's two counters from `merge_result` — one `.add()` per
    distinct (severity, source) pair present in `merge_result.findings`
    (never per-finding free text; only the bounded-cardinality severity/
    source tags, per this spec's privacy/cardinality rules), plus the
    dropped-by-ceiling count, always (even when zero, so the counter's
    absence never has to be interpreted as "zero" versus "not yet
    recorded")."""
    with suppress_telemetry_errors(
        logger, "findings metrics telemetry recording failed (merge result unaffected)"
    ):
        counts: Counter[tuple[str, str, str]] = Counter(
            (finding.severity, finding.source, category_label(finding.category))
            for finding in merge_result.findings
        )
        for (severity, source, category), count in counts.items():
            _findings_count_total.add(
                count, {"severity": severity, "source": source, "category": category}
            )
        dropped: Counter[str] = Counter(
            category_label(finding.category) for finding in merge_result.dropped_by_ceiling
        )
        for category, count in dropped.items():
            _findings_dropped_by_ceiling_total.add(count, {"category": category})
        if not dropped:
            # Zero is still recorded, so "no drops" differs from "not recorded".
            _findings_dropped_by_ceiling_total.add(0, {"category": "none"})


class ShowNitsResolver(Protocol):
    """Reads the org's ``findings_show_nits`` setting —
    a narrow consumer-defined reader, implemented by
    ``tenancy.show_nits.FailClosedShowNitsResolver`` and injected at the
    composition root."""

    async def show_nits(self, org_id: str) -> bool:
        """True when this org opted in to seeing nit-tier findings."""
        ...


def _record_v2_metrics(
    merge_result: MergeResult, inline: list[Finding], collapsed: dict[str, int]
) -> None:
    with suppress_telemetry_errors(
        logger, "v2 layout telemetry recording failed (merge result unaffected)"
    ):
        for tier, count in Counter(effective_tier(f) for f in inline).items():
            _findings_posted_by_tier_total.add(count, {"tier": tier})
        for reason, count in collapsed.items():
            if count:
                _findings_collapsed_total.add(count, {"reason": reason})
        if merge_result.root_cause_merges:
            _findings_deduped_total.add(len(merge_result.root_cause_merges), {"kind": "root_cause"})
        for tier, count in Counter(
            effective_tier(f) for f in merge_result.dropped_by_ceiling
        ).items():
            _findings_dropped_by_ceiling_by_tier_total.add(count, {"tier": tier})


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
        repo_id: str,
        pr_number: int,
        head_sha: str,
        findings: list[Finding],
        collapsed_findings: list[Finding] | None = None,
        unreviewed_files: tuple[str, ...] = (),
    ) -> None:
        """Post one batched PR review carrying every inline comment in a
        single call, per both PRDs' NFR batching decision. Never called
        with an empty ``findings`` list (see this module's own docstring's
        "zero-finding review" note).

        ``repo_id``/``head_sha`` (US-M11)
        are this platform's own domain repo id and the PR's commit sha at
        posting time, needed by the concrete implementation
        (``adapter/github/posting.py``) to persist a
        ``posted_finding_comment`` provenance row per posted comment.
        ``head_sha`` was already threaded through this step's own
        ``FindingsOutputInput`` (used two lines later for
        ``post_check_run``); ``repo_id`` likewise (``FindingsOutputInput.repo_id``)
        — both zero-new-plumbing, same-file changes.

        ``collapsed_findings``: ``None`` is the v1 layout. A list,
        even an empty one, selects the v2 layout: ``findings`` are posted
        inline and ``collapsed_findings`` go in one collapsed summary in the
        review body. Called with an empty ``findings`` list only when
        ``collapsed_findings`` is non-empty.

        ``unreviewed_files`` (spec-diff-batching.md): files no review call
        covered; listed in the review body. The step passes it only when
        non-empty, and may call with empty ``findings`` when it is."""
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
    ``review.verification.VerificationOutput``'s field shape without
    importing it, the same "Import boundary" pattern every downstream step
    already follows for its own upstream dependency (per US-F1;
    ``review/test_step_boundaries.py``).

    Also, deliberately, the shape ``review.pipeline.StepFailureInfo.seed_input``
    is validated against on the *failure* path (``_run_failure_path``
    below) — the raw job payload (``apps/webhook_api/webhook_routes.py``'s
    own ``_review_enqueue_payload``, mirrored exactly by
    ``review.prefilter.PrefilterInput``)
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
    # US6: forwarded from `review.verification.VerificationOutput`'s own new
    # field the same way every other field here already mirrors that
    # step's output. `None` (the default, matching every non-context-builder
    # upstream shape — `StaticAnalysisOutput`, `PrefilterOutput`, and the
    # failure-path `StepFailureInfo.seed_input` alike, none of which carry a
    # `context_bundle` field) means no finding can have a citation resolved
    # against anything, which is exactly correct in that case.
    context_bundle: ContextBundle | None = None
    # Forwarded by upstream steps when present; `None` means "unknown", in which
    # case no inline finding is demoted.
    diff: Diff | None = None
    # Files the review did not cover (diff batching cap or a failed batch).
    unreviewed_files: tuple[str, ...] = ()


def _demote_off_diff(findings: list[Finding], diff: Diff | None) -> tuple[list[Finding], int]:
    """GitHub rejects the whole create-review call (422) when any inline comment's
    line is outside the PR diff, so such findings become file-level (``line=None``)."""
    if diff is None:
        return findings, 0
    allowed = commentable_lines(diff)
    out: list[Finding] = []
    demoted = 0
    for finding in findings:
        if finding.line is not None and finding.line not in allowed.get(finding.file, ()):
            finding = finding.model_copy(update={"line": None})
            demoted += 1
        out.append(finding)
    return out, demoted


class FindingsOutputResult(BaseModel):
    """This step's own output — nothing downstream depends on it (this is
    the pipeline's terminal step), but every other step
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
    module's own docstring for the full design. Registered as the
    ``StepRegistry.terminal_step`` (``apps/worker/main.py``) so it also
    runs on a fatal upstream failure, per ``review.pipeline.StepRegistry``'s
    own "Terminal step" docstring note.
    """

    name = "output"

    def __init__(
        self,
        publisher: FindingsPublisher,
        *,
        depends_on: str = "verification",
        show_nits_resolver: ShowNitsResolver | None = None,
    ) -> None:
        """``depends_on`` names this step's single upstream dependency —
        ``"verification"`` by default (the ordinary, all-steps-enabled case),
        or a fallback further up the chain (``"review_generation"``,
        ``"context_builder"``, ``"static_analysis"``, ...) when the
        composition root (``apps/worker/main.py``) determines a later step
        is disabled.

        **Why this is a constructor parameter, not a fixed class attribute
        (a bug caught in review):** ``review.pipeline.StepRegistry``
        resolves each step's ``input`` from exactly its declared dependency's
        output (``_resolve_input``); a disabled step never produces one. With
        a fixed ``depends_on=("llm_review",)`` (the original
        name for what is now ``review_generation``), disabling
        that step (a supported fallback to static-analysis-only output)
        left this step with no satisfiable dependency, and the registry's own
        generic "no defined behavior for that combination" ``RuntimeError``
        propagated as an unhandled exception, bypassing the terminal-step
        failure-signal mechanism (the "always produce a
        GitHub-visible signal" goal) entirely. The composition root, which
        already knows every step's enablement flag before it builds this
        registry, instead passes the one upstream step actually guaranteed to
        run — ``resolve_review_pipeline_depends_on``/
        ``check_step_configuration_viable`` (``review/config.py``) already
        prove ``static_analysis`` can produce findings whenever every later
        step is disabled, so this is never ambiguous.
        ``VerificationOutput``, ``ReviewGenerationOutput``, and
        ``StaticAnalysisOutput`` share the same `from_attributes`-readable
        field shape `FindingsOutputInput` needs (all carry
        `findings`/`org_id`/`repo_id`/`installation_id`/`repo_full_name`/
        `pr_number`/`head_sha`), so no change to this step's own input
        handling is needed — only which step's output it reads.
        """
        self._publisher = publisher
        self._show_nits_resolver = show_nits_resolver
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
        # `collapsed is None` is the v1 layout; a list is the v2 layout (A8: one
        # switch, snapshotted on the job context).
        v2 = context.review_quality_v2
        merge_result = merge_findings(parsed.findings, tiered=v2)
        hidden_nits = 0
        has_nits = v2 and any(
            effective_tier(f) == "nit"
            for f in (*merge_result.findings, *merge_result.dropped_by_ceiling)
        )
        # Settings lookup only when there is a nit to hide.
        if has_nits and not await self._show_nits(parsed.org_id):
            # Hide after merge so nits are still deduped and counted, and (tiered order
            # cuts nits first) the ceiling for non-nits is unchanged.
            visible = [f for f in merge_result.findings if effective_tier(f) != "nit"]
            cut = [f for f in merge_result.dropped_by_ceiling if effective_tier(f) != "nit"]
            hidden_nits = (len(merge_result.findings) - len(visible)) + (
                len(merge_result.dropped_by_ceiling) - len(cut)
            )
            merge_result = merge_result.model_copy(
                update={
                    "findings": visible,
                    "dropped_by_ceiling": cut,
                    "dropped_by_ceiling_count": len(cut),
                }
            )
        if hidden_nits:
            logger.info(
                "findings-merge: nit findings hidden by org setting",
                extra={"job_id": context.job_id, "hidden_count": hidden_nits},
            )
        _record_findings_metrics(merge_result)
        findings = merge_result.findings
        collapsed: list[Finding] | None = None
        if v2:
            nits = [f for f in findings if effective_tier(f) == "nit"]
            findings = [f for f in findings if effective_tier(f) != "nit"]
            collapsed = nits + merge_result.dropped_by_ceiling
            for kept, folded in merge_result.root_cause_merges:
                logger.info(
                    "findings-merge: root-cause duplicate folded",
                    extra={"job_id": context.job_id, "kept": kept, "folded": folded},
                )
            _record_v2_metrics(
                merge_result,
                findings,
                {
                    "nit": len(nits),
                    "overflow": len(merge_result.dropped_by_ceiling),
                    "nit_hidden": hidden_nits,
                },
            )
        if merge_result.dropped_by_ceiling_count:
            logger.warning(
                "findings-merge: posted-findings ceiling exceeded; dropping excess findings",
                extra={
                    "job_id": context.job_id,
                    "ceiling": POSTED_FINDINGS_CEILING,
                    "dropped_count": merge_result.dropped_by_ceiling_count,
                },
            )

        # US6: for any finding whose `context_ref_ids` resolve to
        # a real citation, build a *new* `Finding` (this model is
        # `frozen=True`) with the citation text set on its own
        # `citation_markdown` field, and use that modified `Finding` from
        # here on — including on the dry-run path below, per this module's
        # own docstring's "dry-run output must reflect... the same objects a
        # real post would use" requirement; this must not run only on the
        # real-post path.
        # v2 only: v1 stays byte-identical to before.
        findings, demoted = _demote_off_diff(findings, parsed.diff if v2 else None)
        if demoted:
            logger.warning(
                "findings-output: inline findings outside the PR diff demoted to file-level",
                extra={"job_id": context.job_id, "demoted_count": demoted},
            )
        findings = self._apply_citations(findings, parsed.context_bundle, job_id=context.job_id)
        reported_count = len(findings) + len(collapsed or [])

        if context.dry_run:
            logger.info(
                "dry-run: would post PR review and check-run",
                extra={
                    "job_id": context.job_id,
                    "pr_number": parsed.pr_number,
                    "comment_count": reported_count,
                    "check_run_conclusion": "neutral",
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                    "collapsed_findings": [
                        finding.model_dump(mode="json") for finding in collapsed or []
                    ],
                    "unreviewed_files": list(parsed.unreviewed_files),
                },
            )
            return FindingsOutputResult(
                comment_count=reported_count, check_run_conclusion="neutral", posted=False
            )

        if findings or collapsed or parsed.unreviewed_files:
            # Passed only when non-empty, so every other call stays exactly as before.
            # Any: the kwargs mix value types, and the publisher is a typed Protocol.
            extra: dict[str, Any] = {}
            if collapsed is not None:
                extra["collapsed_findings"] = collapsed
            if parsed.unreviewed_files:
                extra["unreviewed_files"] = parsed.unreviewed_files
            await self._publisher.post_review(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                repo_id=parsed.repo_id,
                pr_number=parsed.pr_number,
                head_sha=parsed.head_sha,
                findings=findings,
                **extra,
            )

        try:
            await self._publisher.post_check_run(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                head_sha=parsed.head_sha,
                finding_count=reported_count,
            )
        except Exception as exc:
            if reported_count:
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
                "comment_count": reported_count,
                "check_run_conclusion": "neutral",
            },
        )
        return FindingsOutputResult(
            comment_count=reported_count, check_run_conclusion="neutral", posted=True
        )

    async def _show_nits(self, org_id: str) -> bool:
        return self._show_nits_resolver is not None and await self._show_nits_resolver.show_nits(
            org_id
        )

    @staticmethod
    def _apply_citations(
        findings: list[Finding], context_bundle: ContextBundle | None, *, job_id: str
    ) -> list[Finding]:
        """Returns `findings` with any citation-bearing entry replaced by a
        `model_copy`'d `Finding` carrying the rendered citation in its own
        `citation_markdown` field — never mutating a `Finding` in place
        (`frozen=True`) and never appended into `message` (see
        `Finding.citation_markdown`'s own docstring for why: `message` gets
        `escape_markdown`'d by `adapter/github/posting.py`'s own
        `_finding_body`, which would mangle `render_citation`'s own Markdown
        template characters if the two were merged before that pass).

        `context_bundle is None` (no context builder ran, or it ran and
        produced nothing to render — see `FindingsOutputInput`'s own
        docstring) short-circuits to returning `findings` unchanged, since
        no `context_ref_ids` could resolve against anything in that case.
        """

        if context_bundle is None:
            return findings

        result: list[Finding] = []
        for finding in findings:
            if not finding.context_ref_ids:
                result.append(finding)
                continue
            citation_text = render_citation(
                list(finding.context_ref_ids), context_bundle, job_id=job_id
            )
            with suppress_telemetry_errors(
                logger, "citation findings telemetry recording failed (finding unaffected)"
            ):
                _citation_findings_total.add(1, {"cited": str(citation_text is not None).lower()})
            if citation_text is None:
                result.append(finding)
                continue
            result.append(finding.model_copy(update={"citation_markdown": citation_text}))
        return result

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
