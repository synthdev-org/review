"""Unit tests for ``review.memory_suppression.suppress_via_memory`` with fake matcher/embedder
(plan-phase-10 Phase 5). ``test_verification_step.py`` covers the same helper through
``VerificationStep``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from findings.models import Finding
from indexing.embedding import EmbeddingVector
from memory.fingerprint import build_finding_fingerprint
from review.memory_suppression import (
    FingerprintSuppressionConfig,
    MemorySuppressionConfig,
    suppress_via_memory,
)


@dataclass
class _Embedder:
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        self.calls.append(list(texts))
        # The first coordinate encodes the message length so each finding is distinguishable.
        return tuple(EmbeddingVector(values=(float(len(t)), 0.0), dimension=2) for t in texts)


@dataclass
class _Matcher:
    by_first_coordinate: dict[float, float | None]
    repo_ids: list[str] = field(default_factory=list)

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: tuple[float, ...], half_life_days: float, now: datetime
    ) -> float | None:
        self.repo_ids.append(repo_id)
        return self.by_first_coordinate.get(embedding[0])


def _finding(message: str) -> Finding:
    return Finding(
        file="a.py",
        line=1,
        severity="low",
        message=message,
        source="clone_detection",
        rule_id="duplication",
    )


def _config(
    matcher: _Matcher, embedder: _Embedder, threshold: float = 0.9
) -> MemorySuppressionConfig:
    return MemorySuppressionConfig(
        memory_matcher=matcher,
        embedding_client=embedder,
        similarity_threshold=threshold,
        decay_half_life_days=180.0,
        enabled=True,
    )


def _fingerprint_len(finding: Finding) -> float:
    return float(len(f"{finding.file}\n{finding.rule_id}\n{finding.message}"))


@pytest.mark.asyncio
async def test_drops_only_findings_strictly_above_threshold() -> None:
    above, equal, below, none = _finding("a"), _finding("bb"), _finding("ccc"), _finding("dddd")
    matcher = _Matcher(
        {
            _fingerprint_len(above): 0.95,
            _fingerprint_len(equal): 0.9,
            _fingerprint_len(below): 0.5,
            _fingerprint_len(none): None,
        }
    )
    embedder = _Embedder()

    kept, suppressed = await suppress_via_memory(
        [above, equal, below, none], config=_config(matcher, embedder), repo_id="repo-1"
    )

    assert kept == [equal, below, none]
    assert suppressed == 1
    assert matcher.repo_ids == ["repo-1"] * 4
    assert len(embedder.calls) == 1  # one batched embed call


@pytest.mark.asyncio
async def test_empty_input_makes_no_embedding_or_matcher_call() -> None:
    matcher, embedder = _Matcher({}), _Embedder()

    assert await suppress_via_memory([], config=_config(matcher, embedder), repo_id="r") == ([], 0)
    assert embedder.calls == [] and matcher.repo_ids == []


@dataclass
class _ExactMatcher:
    """A FingerprintMatcher fake: dismisses exactly the fingerprint texts it was given."""

    dismissed: set[str]
    asked: list[tuple[str, str]] = field(default_factory=list)

    async def is_dismissed(self, *, repo_id: str, fingerprint: str) -> bool:
        self.asked.append((repo_id, fingerprint))
        return fingerprint in self.dismissed


@pytest.mark.asyncio
async def test_fingerprint_config_drops_exactly_the_dismissed_findings_without_an_embedder() -> (
    None
):
    gone, kept_finding = _finding("same issue"), _finding("other issue")
    matcher = _ExactMatcher(
        {build_finding_fingerprint(file="a.py", rule_id="duplication", message="same issue")}
    )

    kept, suppressed = await suppress_via_memory(
        [gone, kept_finding],
        config=FingerprintSuppressionConfig(fingerprint_matcher=matcher, enabled=True),
        repo_id="repo-1",
    )

    assert kept == [kept_finding]
    assert suppressed == 1
    assert [repo for repo, _ in matcher.asked] == ["repo-1", "repo-1"]


@pytest.mark.asyncio
async def test_fingerprint_config_with_no_findings_asks_nothing() -> None:
    matcher = _ExactMatcher(set())

    result = await suppress_via_memory(
        [],
        config=FingerprintSuppressionConfig(fingerprint_matcher=matcher, enabled=True),
        repo_id="r",
    )

    assert result == ([], 0)
    assert matcher.asked == []
