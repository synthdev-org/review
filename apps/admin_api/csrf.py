"""apps.admin_api.csrf — the session-bound CSRF token Assumption 13
requires: an HMAC of the session id, keyed by a server-side secret
(``AdminAPISettings.admin_csrf_secret``) — not a bare independent random
value, which would remain vulnerable to cross-subdomain cookie injection
("cookie tossing") if this deployment ever shares a parent domain with a
less-trusted subdomain.

The same "value the client hands back must match a server-held
computation, compared via ``hmac.compare_digest``" shape this repo already
uses twice — ``apps/webhook_api/admin_routes.py``'s bearer-token check,
``adapter/github/manifest.py``'s ``generate_setup_state``/
``verify_setup_state`` signed-cookie pair — reused a third time here rather
than a new, independently-reasoned-about primitive.

Pure functions, no FastAPI/HTTP import: ``apps/admin_api/auth_routes.py``
calls ``generate_csrf_token`` at login (to set the CSRF cookie);
``apps/admin_api/dependencies.py``'s ``require_session`` calls
``verify_csrf_token`` on every non-``GET``/``HEAD``/``OPTIONS`` request.
"""

from __future__ import annotations

import hashlib
import hmac


def generate_csrf_token(*, session_id: str, secret: bytes) -> str:
    """The CSRF token for ``session_id`` — deterministic given
    ``(session_id, secret)``, so no server-side storage of the token
    itself is ever needed; ``verify_csrf_token`` below recomputes the same
    value independently rather than looking one up.
    """
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(*, session_id: str, token: str | None, secret: bytes) -> bool:
    """``True`` only if ``token`` (the client-supplied ``X-CSRF-Token``
    header value) matches the HMAC this function independently recomputes
    from ``session_id`` — compared via ``hmac.compare_digest``, never
    ``==``, per this repo's constant-time-comparison convention for a
    security-sensitive string comparison. ``token=None`` (a missing
    header) is always rejected, never treated as a vacuous match.
    """
    if token is None:
        return False
    expected = generate_csrf_token(session_id=session_id, secret=secret)
    return hmac.compare_digest(expected, token)
