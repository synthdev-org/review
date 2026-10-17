"""review.memory_suppression — the Memory similarity-suppression pre-check shared by
``VerificationStep`` (``llm`` findings) and ``CloneDetectionStep`` (``clone_detection``
findings); see ``docs/plans/plan-phase-10.md`` Phase 5 and ADR-0009 decision 3.

Moved verbatim from ``review.verification`` so both steps use one implementation without
importing each other. Each finding's fingerprint (``memory.fingerprint.build_finding_fingerprint``,
the same composition the admin ``approve`` route embeds entries from) is compared via
``DismissedFindingMatcher.best_effective_similarity`` against approved, repo-scoped
``dismissed_finding`` entries; a best match above ``similarity_threshold`` is dropped. A
failure in the embedding call or the matcher query is never caught here: it propagates to
the calling step (verification treats it as fatal; clone detection fails open).

Zero infra imports: binds the domain-defined ``EmbeddingClient`` and
``DismissedFindingMatcher`` Protocols only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from findings.models import Finding
from indexing.embedding import EmbeddingClient
from memory.fingerprint import build_finding_fingerprint
from memory.store import DismissedFindingMatcher


@dataclass(frozen=True)
class MemorySuppressionConfig:
    """The four mutually-dependent fields US-M6/US-M7/US-M9/US-M12's
    similarity-suppression pre-check needs, grouped into one parameter
    rather than four independent ones: ``threshold``/``decay_half_life_days`` are
    meaningless without ``memory_matcher``/``embedding_client`` present together, so
    bundling makes the invalid combination structurally unrepresentable.
    """

    memory_matcher: DismissedFindingMatcher
    embedding_client: EmbeddingClient
    similarity_threshold: float
    decay_half_life_days: float
    enabled: bool


async def suppress_via_memory(
    findings: list[Finding], *, config: MemorySuppressionConfig, repo_id: str
) -> tuple[list[Finding], int]:
    """US-M6/US-M7/US-M9's pre-check. Returns ``(kept_findings, suppressed_count)``.
    Takes ``config`` explicitly: the caller has already checked it is present and
    ``enabled`` (US-M12's kill switch), so there is nothing left to guard here.
    """
    if not findings:
        return [], 0
    fingerprints = [
        build_finding_fingerprint(
            file=finding.file, rule_id=finding.rule_id, message=finding.message
        )
        for finding in findings
    ]
    # `embed` is synchronous and CPU-bound (per `EmbeddingClient`'s docstring), so it runs
    # in a thread to keep the step's event loop free.
    vectors = await asyncio.to_thread(config.embedding_client.embed, fingerprints)

    now = datetime.now(UTC)
    kept: list[Finding] = []
    suppressed_count = 0
    for finding, vector in zip(findings, vectors, strict=True):
        similarity = await config.memory_matcher.best_effective_similarity(
            repo_id=repo_id,
            embedding=vector.values,
            half_life_days=config.decay_half_life_days,
            now=now,
        )
        if similarity is not None and similarity > config.similarity_threshold:
            suppressed_count += 1
            continue
        kept.append(finding)

    return kept, suppressed_count
