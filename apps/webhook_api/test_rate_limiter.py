"""Unit tests for apps.webhook_api.rate_limiter.InMemoryRateLimiter.

Pure logic, no I/O, no FastAPI — a controllable fake clock makes window
expiry deterministic rather than relying on real `time.sleep`.
"""

from __future__ import annotations

import pytest

from apps.webhook_api.rate_limiter import InMemoryRateLimiter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_allows_up_to_max_requests_within_window() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=3, window_seconds=60, clock=clock)

    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert limiter.allow("client-a")


def test_rejects_once_max_requests_exceeded_within_window() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60, clock=clock)

    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")


def test_allows_again_once_window_elapses() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=10, clock=clock)

    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")

    clock.advance(10.001)

    assert limiter.allow("client-a")


def test_keys_are_tracked_independently() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60, clock=clock)

    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")
    assert limiter.allow("client-b")


def test_rejects_invalid_max_requests() -> None:
    with pytest.raises(ValueError, match="max_requests"):
        InMemoryRateLimiter(max_requests=0, window_seconds=60)


def test_rejects_invalid_window_seconds() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        InMemoryRateLimiter(max_requests=1, window_seconds=0)
