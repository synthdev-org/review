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

import itertools
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
        # Each hit is (timestamp, token): the token disambiguates concurrent
        # reservations for the same key so `release()` can undo the exact
        # reservation a caller made, instead of whichever hit happens to be
        # most recent when `release()` runs (see `reserve`/`release`).
        self._hits: dict[str, deque[tuple[float, int]]] = {}
        self._next_token = itertools.count()
        self._calls_since_sweep = 0

    # `_hits` gains one entry per distinct key ever seen (client IP, or
    # repo id for the enqueue limiter) and nothing ever removes a key once
    # its deque empties — only entries a key's *own* calls touch are ever
    # pruned. In a long-running process serving many distinct keys over
    # time, that is unbounded growth. There's no external scheduler to
    # hook a periodic sweep into at this phase, so `allow()` triggers a
    # full sweep of `_hits` (dropping every key whose window has gone
    # idle) every `_SWEEP_INTERVAL` calls — self-contained, no caller
    # wiring required.
    _SWEEP_INTERVAL = 512

    def allow(self, key: str) -> bool:
        """Record and permit a call for ``key``, or reject it if ``key``
        has already made ``max_requests`` calls within the trailing
        window. Returns ``True``/``False`` rather than raising — the caller
        decides how to respond (see ``apps/webhook_api/webhook_routes.py``).

        Callers that may need to undo their own reservation later (because
        the work it gates can still fail) should use ``reserve()`` instead,
        which returns the token ``release()`` needs to identify *this*
        call's own hit rather than whichever hit is most recent when
        ``release()`` runs — see ``reserve``'s docstring."""
        allowed, _token = self.reserve(key)
        return allowed

    def reserve(self, key: str) -> tuple[bool, int | None]:
        """Like ``allow()``, but also returns a token identifying this
        specific reservation (``None`` if not allowed) so a later
        ``release(key, token)`` can undo exactly *this* call's hit — not
        just whichever hit happens to be most recent for ``key`` at release
        time. Without this, two concurrent reservations for the same key
        (e.g. two webhook deliveries for the same repo racing across an
        ``await``) could have one release the other's hit instead of its
        own."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())

        cutoff = now - self._window_seconds
        while hits and hits[0][0] <= cutoff:
            hits.popleft()

        if len(hits) >= self._max_requests:
            allowed = False
            token = None
        else:
            # Recorded before the periodic sweep below runs, so a sweep
            # landing on this exact call never finds this key's deque
            # still empty and evicts it out from under this hit — see
            # `_sweep`'s own docstring for why that ordering matters.
            token = next(self._next_token)
            hits.append((now, token))
            allowed = True

        self._calls_since_sweep += 1
        if self._calls_since_sweep >= self._SWEEP_INTERVAL:
            self._sweep(now)

        return allowed, token

    def release(self, key: str, token: int | None = None) -> None:
        """Undo a successful ``allow()``/``reserve()`` call for ``key``.

        For a caller that consumes a slot before attempting the work it
        gates (e.g. reserving a rate-limit slot before an enqueue that then
        fails and rolls back) — without this, a mid-transaction failure
        would permanently burn a slot for work that never actually
        happened, even though the caller's own retry (or GitHub's webhook
        redelivery) represents the same logical event, not a new one.

        When ``token`` is given (from ``reserve()``), removes exactly that
        reservation, correct even if other concurrent calls for the same
        key have recorded hits in between. Without a token, falls back to
        popping the most recent hit (legacy behavior for simple ``allow()``
        callers with no concurrent access to the same key). Safe to call
        even if the key has no recorded hits, or the token is no longer
        present — a no-op."""
        hits = self._hits.get(key)
        if not hits:
            return
        if token is None:
            hits.pop()
            return
        for index, (_timestamp, hit_token) in enumerate(hits):
            if hit_token == token:
                del hits[index]
                return

    def _sweep(self, now: float) -> None:
        """Drop every tracked key whose deque has gone fully idle."""
        self._calls_since_sweep = 0
        cutoff = now - self._window_seconds
        stale_keys = []
        for key, hits in self._hits.items():
            while hits and hits[0][0] <= cutoff:
                hits.popleft()
            if not hits:
                stale_keys.append(key)
        for key in stale_keys:
            del self._hits[key]
