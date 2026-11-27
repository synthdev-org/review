"""Run: `python -m apps.local_review [--base main] [--head HEAD] [--repo .]`.

Config comes from `.env.local` (gitignored) or `REVIEW_LOCAL_*` env vars.
Runs prefilter -> review_generation -> verification -> output. Static
analysis, context builder (needs Postgres), intent summary and spec review
are disabled; the classifier (per-org, DB-backed) is not wired.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from adapter.classifier.typesafe import BreakerRegistry, TypeSafeClassifier
from adapter.egress.transport import EgressPolicy, ProviderCredential
from adapter.llm.litellm_client import LiteLLMClient
from adapter.localgit.commits import LocalGitCommitSource
from adapter.localgit.diff import LocalGitDiffFetcher
from findings.models import Finding
from findings.redact import NoOpTextRedactor
from llmgateway.classifier import ClassificationRequest, ClassificationResponse, Classifier
from review.config import StepEnablement, StepTimeouts, resolve_review_pipeline_depends_on
from review.diff_classify import ShrinkConfig
from review.diff_expand import ExpandConfig
from review.finding_judge import ClassifierSelection, ClassifierVerification
from review.output import FindingsOutputStep
from review.pipeline import FailurePolicy, PipelineFailedError, StepRegistry
from review.prefilter import PrefilterStep
from review.review_generation import (
    DiffBatchingConfig,
    ReviewContextConfig,
    ReviewGenerationStep,
)
from review.step import StepContext
from review.verification import VerificationFileContext, VerificationStep
from review.verification_context import FileContextConfig


class LocalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVIEW_LOCAL_", env_file=".env.local", extra="ignore"
    )

    llm_endpoint: str
    llm_model: str
    llm_api_key: SecretStr
    llm_timeout_seconds: float = 120
    llm_max_diff_bytes: int = 200_000
    max_diff_chunks: int = 8
    diff_batch_concurrency: int = 4
    diff_batch_link_timeout_seconds: int = 15
    diff_batch_link_grouping: bool = True  # False = no import or call links
    # Same names and defaults as the worker's (docs/specs/spec-review-context.md).
    review_shrink_enabled: bool = True
    review_stub_data_min_bytes: int = 8000
    review_prose_max_bytes: int = 12000
    review_risk_ordering_enabled: bool = True
    review_pr_map_enabled: bool = True
    review_pr_map_max_bytes: int = 8000
    review_hunk_expansion_enabled: bool = False
    verification_file_context_enabled: bool = False
    verification_file_context_max_bytes: int = 24000
    verification_file_context_total_bytes: int = 80000
    verification_file_context_timeout_seconds: int = 10
    review_commit_mode_enabled: bool = False
    review_commit_max_commits: int = 60
    # Jev (TypeSafe) classifier verification; on only when a key is set.
    llm_verify_model: str | None = None  # defaults to llm_model
    jev_api_key: SecretStr | None = None
    jev_endpoint: str = "https://api.typesafe.ai"
    jev_model: str = "jev-1.14.0"
    jev_mode: Literal["shadow", "active"] = "active"
    jev_timeout_seconds: float = 10
    review_commit_fetch_timeout_seconds: int = 30


class PrintingPublisher:
    """`FindingsPublisher` that writes to stdout instead of GitHub."""

    @staticmethod
    def _show(finding: Finding) -> None:
        where = f"{finding.file}:{finding.line}" if finding.line else finding.file
        print(f"\n[{finding.severity}] {where}\n  {finding.message}")

    async def post_review(
        self,
        *,
        findings: list[Finding],
        collapsed_findings: list[Finding] | None = None,
        unreviewed_files: tuple[str, ...] = (),
        **_: object,
    ) -> None:
        for finding in findings:
            self._show(finding)
        for finding in collapsed_findings or []:
            self._show(finding)
        if unreviewed_files:
            print("\nNot reviewed:\n  " + "\n  ".join(unreviewed_files))

    async def post_check_run(self, *, finding_count: int, **_: object) -> None:
        print(f"\n--- {finding_count} finding(s) ---")

    async def post_failure_check_run(
        self, *, failed_step_name: str, error: str, **_: object
    ) -> None:
        print(f"\n--- FAILED in {failed_step_name}: {error} ---", file=sys.stderr)


class RecordingClassifier:
    """Wraps a `Classifier` to count calls and keep each answer set, so the
    local run can show what Jev was asked and what it said.
    """

    def __init__(self, inner: Classifier) -> None:
        self._inner = inner
        self.calls = 0
        self.errors = 0
        self.records: list[tuple[str, dict[str, float]]] = []

    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        self.calls += 1
        try:
            response = await self._inner.classify(request, timeout=timeout)
        except Exception:
            self.errors += 1
            raise
        # The state is "<framing>Finding in <file> (<sev>): <message>\n\nDiff hunk:...".
        finding = request.state.split("Finding in ", 1)[-1].split("\n\nDiff hunk:", 1)[0]
        self.records.append((finding, {q: round(a.p, 2) for q, a in response.answers.items()}))
        return response


class StaticClassifierResolver:
    """`ClassifierSelectionResolver` returning one fixed selection for every org."""

    def __init__(self, selection: ClassifierSelection) -> None:
        self._selection = selection

    async def resolve(self, org_id: str) -> ClassifierSelection | None:
        return self._selection


def _build_classifier(
    settings: LocalSettings,
) -> tuple[ClassifierVerification, RecordingClassifier] | None:
    if settings.jev_api_key is None:
        return None
    host = settings.jev_endpoint.split("//", 1)[-1].split("/", 1)[0]
    recording = RecordingClassifier(
        TypeSafeClassifier(
            settings.jev_endpoint,
            credential=ProviderCredential(
                ref="jev", token=settings.jev_api_key, allowed_hosts=(host,)
            ),
            policy=EgressPolicy(allow_private_networks=False),
            breaker=BreakerRegistry().for_key(settings.jev_endpoint, "jev"),
        )
    )
    selection = ClassifierSelection(
        mode=settings.jev_mode, model=settings.jev_model, classifier=recording
    )
    wiring = ClassifierVerification(
        resolver=StaticClassifierResolver(selection),
        redactor=NoOpTextRedactor(),
        redaction_slots=asyncio.Semaphore(4),
        classifier_slots=asyncio.Semaphore(8),
        max_state_bytes=8000,
        timeout=settings.jev_timeout_seconds,
    )
    return wiring, recording


def _build_review_context(
    settings: LocalSettings, commits: LocalGitCommitSource | None
) -> ReviewContextConfig:
    return ReviewContextConfig(
        shrink=(
            ShrinkConfig(
                stub_data_min_bytes=settings.review_stub_data_min_bytes,
                prose_max_bytes=settings.review_prose_max_bytes,
            )
            if settings.review_shrink_enabled
            else None
        ),
        expand=ExpandConfig() if settings.review_hunk_expansion_enabled else None,
        pr_map_max_bytes=settings.review_pr_map_max_bytes if settings.review_pr_map_enabled else 0,
        risk_ordering=settings.review_risk_ordering_enabled,
        commits=commits if settings.review_commit_mode_enabled else None,
        commit_max_commits=settings.review_commit_max_commits,
        commit_fetch_timeout_seconds=float(settings.review_commit_fetch_timeout_seconds),
    )


def _build_file_context(
    settings: LocalSettings, fetcher: LocalGitDiffFetcher
) -> VerificationFileContext | None:
    if not settings.verification_file_context_enabled:
        return None
    return VerificationFileContext(
        fetcher,
        FileContextConfig(
            max_bytes_per_file=settings.verification_file_context_max_bytes,
            max_total_bytes=settings.verification_file_context_total_bytes,
        ),
        float(settings.verification_file_context_timeout_seconds),
    )


def _build_timeouts(
    settings: LocalSettings,
    context: ReviewContextConfig,
    file_context: VerificationFileContext | None,
) -> Mapping[str, float]:
    def waves(calls: int) -> int:
        return math.ceil(calls / settings.diff_batch_concurrency)

    return StepTimeouts(
        review_generation_step_timeout_seconds=settings.diff_batch_link_timeout_seconds
        + waves(settings.max_diff_chunks) * int(settings.llm_timeout_seconds)
        + (settings.review_commit_fetch_timeout_seconds if context.commits else 0)
        + 30,
        verification_step_timeout_seconds=waves(settings.max_diff_chunks + 1)
        * int(settings.llm_timeout_seconds)
        * 3
        + (settings.verification_file_context_timeout_seconds if file_context else 0),
    ).as_mapping()


def _build_generation_step(
    settings: LocalSettings,
    llm: LiteLLMClient,
    fetcher: LocalGitDiffFetcher,
    resolved: Mapping[str, str],
    context: ReviewContextConfig,
) -> ReviewGenerationStep:
    return ReviewGenerationStep(
        llm,
        redactor=NoOpTextRedactor(),
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_diff_bytes=settings.llm_max_diff_bytes,
        depends_on=resolved["review_generation"],
        batching=DiffBatchingConfig(
            fetcher,
            settings.max_diff_chunks,
            settings.diff_batch_concurrency,
            float(settings.diff_batch_link_timeout_seconds),
            settings.diff_batch_link_grouping,
        ),
        context=context,
    )


def _build_registry(
    settings: LocalSettings,
    fetcher: LocalGitDiffFetcher,
    commits: LocalGitCommitSource | None = None,
    classifier: ClassifierVerification | None = None,
) -> StepRegistry:
    enablement = StepEnablement(
        static_analysis_enabled=False,
        context_builder_enabled=False,
        intent_summary_enabled=False,
        spec_review_enabled=False,
    )
    resolved = resolve_review_pipeline_depends_on(enablement)

    context = _build_review_context(settings, commits)
    file_context = _build_file_context(settings, fetcher)
    llm = LiteLLMClient(
        endpoint=settings.llm_endpoint, api_key=settings.llm_api_key.get_secret_value()
    )
    return StepRegistry(
        steps=[
            PrefilterStep(fetcher),
            _build_generation_step(settings, llm, fetcher, resolved, context),
            VerificationStep(
                llm,
                model=settings.llm_verify_model or settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                max_diff_bytes=settings.llm_max_diff_bytes,
                depends_on=resolved["verification"],
                batch_concurrency=settings.diff_batch_concurrency,
                file_context=file_context,
                classifier=classifier,
            ),
            FindingsOutputStep(PrintingPublisher(), depends_on=resolved["output"]),
        ],
        enabled=enablement.as_mapping(),
        timeouts=_build_timeouts(settings, context, file_context),
        on_failure=FailurePolicy.FATAL,
        terminal_step="output",
    )


async def _run(repo: Path, base: str, head: str) -> int:
    settings = LocalSettings()  # required fields come from the env
    seed = {
        "org_id": "local",
        "repo_id": "local",
        "installation_id": 0,
        "repo_full_name": f"local/{repo.resolve().name}",
        "pr_number": 0,
        "head_sha": head,
        "base_sha": base,
        "private": True,
    }
    jev = _build_classifier(settings)
    registry = _build_registry(
        settings,
        LocalGitDiffFetcher(repo, base, head),
        LocalGitCommitSource(repo, base, head) if settings.review_commit_mode_enabled else None,
        jev[0] if jev else None,
    )
    try:
        await registry.run(seed, StepContext(job_id="local"))
    except PipelineFailedError as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if jev:
            print(f"jev: {jev[1].calls} call(s), {jev[1].errors} error(s)", file=sys.stderr)
            for finding, answers in jev[1].records:
                print(
                    "jev-answer: " + json.dumps({"finding": finding, "answers": answers}),
                    file=sys.stderr,
                )
    return 0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", default="main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING))
    sys.exit(asyncio.run(_run(args.repo, args.base, args.head)))


if __name__ == "__main__":
    main()
