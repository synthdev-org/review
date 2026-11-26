"""Unit tests for memory.decay — the pure, no-I/O US-M9 decay-math
function, per ADR-0005 (pure domain logic gets Unit tests, no fake/adapter
needed) and docs/rules/testing.md's "new domain logic a story touches still
needs unit tests" rule. No real I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory.decay import effective_similarity

_APPROVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_zero_days_since_approval_returns_raw_similarity_unchanged() -> None:
    result = effective_similarity(
        0.95, approved_at=_APPROVED_AT, half_life_days=180.0, now=_APPROVED_AT
    )

    assert result == pytest.approx(0.95)


def test_one_half_life_halves_the_similarity() -> None:
    # US-M9's own acceptance shape: raw_cosine_similarity * 0.5 ** (N / half_life).
    now = _APPROVED_AT + timedelta(days=180)

    result = effective_similarity(0.95, approved_at=_APPROVED_AT, half_life_days=180.0, now=now)

    assert result == pytest.approx(0.475)


def test_two_half_lives_quarters_the_similarity() -> None:
    now = _APPROVED_AT + timedelta(days=360)

    result = effective_similarity(0.95, approved_at=_APPROVED_AT, half_life_days=180.0, now=now)

    assert result == pytest.approx(0.2375)


def test_a_quarter_of_a_half_life_decays_proportionally() -> None:
    # 45 of 180 days = a quarter of one half-life:
    # 0.80 * 0.5 ** 0.25 == 0.6727171322... (hand-computed).
    now = _APPROVED_AT + timedelta(days=45)

    result = effective_similarity(0.80, approved_at=_APPROVED_AT, half_life_days=180.0, now=now)

    assert result == pytest.approx(0.6727171322, rel=1e-9)


def test_fractional_day_decays_smoothly_not_truncated_to_whole_days() -> None:
    # Half a day into a 2-day half-life: 1.0 * 0.5 ** 0.25 == 0.8408964153.
    now = _APPROVED_AT + timedelta(hours=12)

    result = effective_similarity(1.0, approved_at=_APPROVED_AT, half_life_days=2.0, now=now)

    assert result == pytest.approx(0.8408964153, rel=1e-9)
