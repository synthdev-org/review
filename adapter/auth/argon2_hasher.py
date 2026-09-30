"""adapter.auth.argon2_hasher — Argon2id password hashing for
``apps/admin_api``'s local login, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 1 section (Assumptions 1,
8, 14).

A concrete class, not a ``typing.Protocol`` — no second implementation is
in view, per ``docs/rules/architecture.md``'s "no interface without a
second real implementation already in view" rule (Assumption 1); this
repo's own closest precedent for a comparable secret-verification concern
(``apps/webhook_api/admin_routes.py``'s inline ``hmac.compare_digest``
check) likewise uses no interface at all.

Cost parameters are explicit, not left to ``argon2-cffi``'s own
constructor defaults (Assumption 14) — OWASP's current Argon2id baseline
(19 MiB memory, 2 iterations, 1 degree of parallelism), stated here rather
than assumed, so a future ``argon2-cffi`` release changing its own
defaults can never silently weaken (or strengthen, in a way that changes
this process's CPU/memory budget without anyone deciding to) every hash
this adapter produces.

``verify_dummy`` closes US-A1's username-enumeration timing side channel:
the login route (``apps/admin_api/auth_routes.py``) calls this — a real
Argon2 verification against a fixed, module-level placeholder hash/password
pair, at the same cost as a genuine verification — whenever the submitted
username doesn't exist, so a nonexistent-username response and a
wrong-password response take equivalent time. Argon2's own constant-time
comparison *within* a single verification call is not what this closes;
without this, the nonexistent-username path would skip the (deliberately
expensive) Argon2 computation entirely and return measurably faster than
the wrong-password path, which does run it.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2 import exceptions as argon2_exceptions

# OWASP's current Argon2id baseline (Password Storage Cheat Sheet, "m=19456
# (19 MiB), t=2, p=1"), per Assumption 14 — stated explicitly rather than
# left to argon2-cffi's own constructor defaults.
_OWASP_MEMORY_COST_KIB = 19 * 1024
_OWASP_TIME_COST = 2
_OWASP_PARALLELISM = 1

# A fixed, non-secret placeholder pair `verify_dummy` below always verifies
# successfully against — its only job is to make the nonexistent-username
# login path pay the same Argon2 CPU cost a real (successful or failed)
# verification pays, per this module's own docstring. Never derived from
# any real credential; a module-level constant, not a per-call random
# value, since the whole point is a *fixed*, cacheable-by-the-CPU-branch-
# predictor cost, not a fresh one each time.
_DUMMY_PASSWORD = "review-admin-api-dummy-password-for-timing-safety"


class Argon2PasswordHasher:
    """Hashes and verifies passwords with Argon2id, at explicit
    OWASP-baseline cost parameters.
    """

    def __init__(self) -> None:
        self._hasher = _Argon2PasswordHasher(
            memory_cost=_OWASP_MEMORY_COST_KIB,
            time_cost=_OWASP_TIME_COST,
            parallelism=_OWASP_PARALLELISM,
        )
        # Computed once per process, at construction time — not per call —
        # since `hash()` is itself the expensive operation `verify_dummy`
        # exists to make every rejected-nonexistent-username login pay
        # anyway; computing it lazily on first use would make exactly that
        # first request slower than every subsequent one, a timing tell of
        # its own.
        self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        """Hash ``password`` with Argon2id at this instance's configured
        cost parameters. A synchronous, CPU-bound call — every caller in
        this codebase runs it via ``asyncio.to_thread``, the same
        "never awaited directly from an async route handler" convention
        ``apps/webhook_api/admin_routes.py``'s own embedding-inference call
        already establishes for a comparable CPU-bound operation.
        """
        return self._hasher.hash(password)

    def verify(self, *, password_hash: str, password: str) -> bool:
        """``True`` if ``password`` matches ``password_hash``, ``False``
        for any mismatch or malformed/foreign hash — never raises for a
        genuine verification failure, so callers can treat this as a plain
        boolean check rather than needing to catch argon2-cffi's own
        exception types themselves.
        """
        try:
            self._hasher.verify(password_hash, password)
        except argon2_exceptions.VerificationError:
            return False
        except argon2_exceptions.InvalidHashError:
            return False
        return True

    def verify_dummy(self) -> None:
        """Runs one real Argon2id verification, at the same cost as
        ``verify()``, against a fixed placeholder hash this instance
        always matches — see this module's own docstring for why.
        Deliberately returns ``None`` (never ``bool``): the outcome is
        never meaningful (it always "succeeds" by construction), only the
        CPU cost paid to compute it is.
        """
        self._hasher.verify(self._dummy_hash, _DUMMY_PASSWORD)
