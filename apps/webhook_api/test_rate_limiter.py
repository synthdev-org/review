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


def test_release_frees_the_most_recent_hit_for_a_key() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60, clock=clock)

    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")

    limiter.release("client-a")

    assert limiter.allow("client-a")


def test_release_on_a_key_with_no_hits_is_a_no_op() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60, clock=clock)

    limiter.release("never-called")

    assert limiter.allow("never-called")


def test_release_with_token_frees_exactly_that_reservation() -> None:
    """A caller that reserved with `reserve()` and later releases with its
    own token must free its own slot even if another reservation for the
    same key was made in between — not whichever hit happens to be most
    recent (see `test_release_without_token_can_free_the_wrong_hit`)."""
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60, clock=clock)

    allowed_a, token_a = limiter.reserve("client-a")
    assert allowed_a
    allowed_b, _token_b = limiter.reserve("client-a")
    assert allowed_b

    # Both slots are consumed; a third reservation is rejected.
    assert not limiter.allow("client-a")

    # Release request A's own reservation specifically.
    limiter.release("client-a", token_a)

    # A's slot is free again; B's reservation is untouched.
    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")


def test_release_without_token_can_free_the_wrong_hit() -> None:
    """Documents the legacy no-token fallback: it pops whichever hit is
    most recent, which is wrong under concurrent reservations for the same
    key — this is why `webhook_routes.py` uses `reserve()`/`release(key,
    token)` instead of bare `allow()`/`release(key)`."""
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=2, window_seconds=60, clock=clock)

    allowed_a, token_a = limiter.reserve("client-a")
    assert allowed_a
    allowed_b, _token_b = limiter.reserve("client-a")
    assert allowed_b

    # Release without a token frees the most-recently-reserved hit (B's),
    # not necessarily the caller's own (A's) — the exact hazard the token
    # form of release() exists to avoid.
    limiter.release("client-a")

    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")

    # A's original token is still present and independently releasable.
    limiter.release("client-a", token_a)
    assert limiter.allow("client-a")


def test_hit_recorded_during_a_sweep_is_not_lost() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60, clock=clock)

    # Drive `_calls_since_sweep` to the last call before a sweep fires,
    # using distinct keys so none of them touch the key under test.
    for i in range(limiter._SWEEP_INTERVAL - 1):
        assert limiter.allow(f"warmup-{i}")

    # This call for a brand-new key both records its own first hit *and*
    # triggers the periodic sweep in the same call — the sweep must not
    # evict the hit this call just recorded.
    assert limiter.allow("client-a")

    # If the hit above were lost, this second call would also be allowed.
    assert not limiter.allow("client-a")


def test_rejects_invalid_max_requests() -> None:
    with pytest.raises(ValueError, match="max_requests"):
        InMemoryRateLimiter(max_requests=0, window_seconds=60)


def test_rejects_invalid_window_seconds() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        InMemoryRateLimiter(max_requests=1, window_seconds=0)
