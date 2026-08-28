"""apps.webhook_api.rate_limiter — a basic, per-instance, in-process rate
limiter placed in front of the webhook receiver's signature verification.

Per the plan's Phase 4 section: "The rate limit is per-instance (in-process
counter) — adequate for Phase 0's single-Webhook-API-pod Docker Compose
target; a shared/distributed limiter for multi-replica production
deployments is a Phase 8 (roadmap) hardening concern, noted here so it
isn't mistaken for already solved." This is deliberately a simple
sliding-window counter held in process memory, not backed by
Redis/Postgres/etc. — swapping in a distributed limiter later needs no
change to the webhook route beyond swapping this class out.

Lives in ``apps/webhook_api`` (not ``adapter/github``) because it is
generic HTTP-endpoint protection, not GitHub-specific — it has nothing to
do with GitHub's payload/signature format and is not expected to gain a
second, GitHub-specific implementation.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class InMemoryRateLimiter:
    """Allows at most ``max_requests`` calls to ``allow()`` for a given
    ``key`` within any trailing ``window_seconds`` interval (a sliding
    window, not a fixed bucket that resets on a clock boundary).

    Not safe for use across multiple processes/pods — see the module
    docstring. Safe within a single asyncio event loop process, which is
    this phase's only deployment target.
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record and permit a call for ``key``, or reject it if ``key``
        has already made ``max_requests`` calls within the trailing
        window. Returns ``True``/``False`` rather than raising — the caller
        decides how to respond (see ``apps/webhook_api/webhook_routes.py``)."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())

        cutoff = now - self._window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self._max_requests:
            return False

        hits.append(now)
        return True
