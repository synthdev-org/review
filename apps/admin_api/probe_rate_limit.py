"""
apps.admin_api.probe_rate_limit: the one limiter every Admin probe route shares.

``SlidingWindow`` is the Admin API's local copy of
``apps/webhook_api/rate_limiter.InMemoryRateLimiter``'s core (the lineage the
product-knowledge route used to carry privately): importing it would make one
entrypoint package depend on another (architecture.md). It needs ``has_room``
and ``record`` as separate steps, which ``InMemoryRateLimiter`` does not offer.

``ProbeRateLimiter`` charges a session, a user, the whole pod and (when given) an
org together, so a rejection by any window never burns a slot in the others. One
instance is shared by the product-knowledge and classifier test routes, so both
spend the same budget. In-memory: the quota is per Admin API pod.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Final

from fastapi import HTTPException, Response

from apps.admin_api.dependencies import AuthenticatedSession

__all__ = [
    "GLOBAL_LIMIT_PER_MINUTE",
    "SESSION_LIMIT_PER_MINUTE",
    "ProbeRateLimiter",
    "SlidingWindow",
]

SESSION_LIMIT_PER_MINUTE: Final = 6
GLOBAL_LIMIT_PER_MINUTE: Final = 30


class SlidingWindow:
    """Per-instance sliding-window limiter."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def has_room(self, key: str) -> bool:
        """Check without consuming a slot."""
        now = self._clock()
        hits = self._hits.get(key)  # a check never allocates state for an arbitrary key
        if hits is None:
            return self._limit > 0
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        return len(hits) < self._limit

    def record(self, key: str) -> None:
        """Consume a slot (call only after ``has_room`` on every limiter)."""
        self._hits.setdefault(key, deque()).append(self._clock())
        if len(self._hits) > 1024:  # bound the key set: sweep keys idle for a full window
            cutoff = self._clock() - self._window
            for stale in [
                k for k, v in self._hits.items() if k != key and (not v or v[-1] <= cutoff)
            ]:
                del self._hits[stale]


class ProbeRateLimiter:
    """Session, user, org and pod-wide windows, checked together before any is charged."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Build the four windows on one clock."""
        self._session = SlidingWindow(limit=SESSION_LIMIT_PER_MINUTE, clock=clock)
        # Per *user* too: a fresh login (new session id) must not reset the quota.
        self._user = SlidingWindow(limit=SESSION_LIMIT_PER_MINUTE, clock=clock)
        self._org = SlidingWindow(limit=SESSION_LIMIT_PER_MINUTE, clock=clock)
        self._global = SlidingWindow(limit=GLOBAL_LIMIT_PER_MINUTE, clock=clock)

    def charge(
        self,
        authenticated: AuthenticatedSession,
        response: Response,
        *,
        org_id: str | None = None,
    ) -> None:
        """Take one slot from every window or raise 429 without taking any."""
        windows = [
            (self._session, str(authenticated.session_id)),
            (self._user, str(authenticated.user.id)),
            (self._global, "global"),
        ]
        if org_id is not None:
            windows.append((self._org, org_id))
        if not all(window.has_room(key) for window, key in windows):
            response.headers["Retry-After"] = "60"
            raise HTTPException(status_code=429, detail="rate_limited")
        for window, key in windows:
            window.record(key)
