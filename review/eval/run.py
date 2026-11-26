"""Drive the real ``ReviewGenerationStep`` and ``VerificationStep`` over the
corpus through a given ``LLMClient`` and report per-row outcomes.

Per source PR: one generation call for the rows that carry a diff, then one
verification call over those findings plus the diff-less rows (which feed the
judge only).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from findings.models import Finding
from findings.redact import NoOpTextRedactor
from ingestion.diff import Diff, DiffFile
from llmgateway.client import LLMClient
from review.eval.corpus import EvalRow, group_by_pr
from review.eval.metrics import Outcome
from review.review_generation import ReviewGenerationStep
from review.step import StepContext
from review.verification import VerificationStep

_TIMEOUT = 60.0
_MAX_DIFF_BYTES = 200_000


@dataclass
class _Upstream:
    """Structural stand-in for the upstream step output (the steps validate
    their input with ``from_attributes=True``)."""

    diff: Diff
    findings: list[Finding] = field(default_factory=list)
    org_id: str = "eval-org"
    repo_id: str = "eval-repo"
    installation_id: int = 1
    repo_full_name: str = "eval/corpus"
    pr_number: int = 1
    head_sha: str = "0" * 40


def _finding(row: EvalRow) -> Finding:
    return Finding(
        file=row.file,
        line=row.line,
        severity=row.severity,
        message=row.finding_text,
        source="llm",
    )


async def run_eval(
    rows: list[EvalRow], llm_client: LLMClient, *, model: str, review_quality_v2: bool = False
) -> list[Outcome]:
    generation = ReviewGenerationStep(
        llm_client,
        redactor=NoOpTextRedactor(),
        model=model,
        timeout=_TIMEOUT,
        max_diff_bytes=_MAX_DIFF_BYTES,
        smell_baseline_enabled=False,
    )
    verification = VerificationStep(
        llm_client, model=model, timeout=_TIMEOUT, max_diff_bytes=_MAX_DIFF_BYTES
    )
    outcomes: list[Outcome] = []
    for pr, pr_rows in group_by_pr(rows):
        context = StepContext(job_id=f"eval-pr-{pr}", review_quality_v2=review_quality_v2)
        with_diff = [r for r in pr_rows if r.diff_excerpt is not None]
        diff = Diff(
            files=tuple(
                DiffFile(path=r.file, status="modified", patch=r.diff_excerpt) for r in with_diff
            )
        )
        candidates: list[Finding] = []
        if with_diff:
            generated = await generation.run(_Upstream(diff=diff), context)
            candidates.extend(generated.findings)
        candidates.extend(_finding(r) for r in pr_rows if r.diff_excerpt is None)

        verified = await verification.run(_Upstream(diff=diff, findings=candidates), context)
        kept = {(f.file, f.line) for f in verified.findings}
        outcomes.extend(Outcome(r, kept=(r.file, r.line) in kept) for r in pr_rows)
    return outcomes
