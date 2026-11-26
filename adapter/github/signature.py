"""adapter.github.signature — GitHub webhook HMAC-SHA256 signature
verification.

Verification uses stdlib ``hmac.compare_digest`` for a constant-time
comparison against the computed HMAC-SHA256 digest; a plain ``==``
comparison is a timing-attack vector that lets an attacker recover a valid
signature byte-by-byte. This is the platform's first trust boundary, and
any request without a valid signature must be rejected before any other
processing runs.

Pure stdlib (``hmac``/``hashlib``) — no HTTP layer, no FastAPI import — so
it is directly unit-testable, and importable
from ``apps/webhook_api``'s route without pulling in any adapter-side I/O.
"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="


def verify_webhook_signature(*, payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Return ``True`` iff ``signature_header`` (the raw
    ``X-Hub-Signature-256`` header value) is a valid signature of
    ``payload`` under ``secret``.

    Returns ``False`` — never raises — for a missing header, a header in
    the wrong format, or a signature that doesn't match; callers decide how
    to log/report the specific rejection reason (see
    ``apps/webhook_api/webhook_routes.py``). The final comparison uses
    ``hmac.compare_digest`` for constant-time comparison, per this module's
    docstring above — a plain ``==`` here would let an attacker recover a
    valid signature one byte at a time via timing side channels.
    """
    if not signature_header:
        return False
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False

    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_digest, expected_digest)
