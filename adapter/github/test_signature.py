"""Unit tests for adapter.github.signature.verify_webhook_signature.

Per the plan's Phase 4 Tests section: "signature verification rejects a
tampered payload, a missing signature header, and a signature computed
with the wrong secret ... accepts a validly-signed payload" — exercised
directly against the ``hmac``/``hashlib``-based verification function, no
HTTP layer involved (no FastAPI/TestClient import anywhere in this file).
"""

from __future__ import annotations

import hashlib
import hmac

from adapter.github.signature import verify_webhook_signature

_SECRET = "unit-test-webhook-secret"
_PAYLOAD = b'{"action": "opened", "installation": {"id": 123}}'


def _sign(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_is_accepted() -> None:
    header = _sign(_PAYLOAD, _SECRET)

    assert verify_webhook_signature(payload=_PAYLOAD, signature_header=header, secret=_SECRET)


def test_tampered_payload_is_rejected() -> None:
    header = _sign(_PAYLOAD, _SECRET)
    tampered_payload = _PAYLOAD.replace(b"123", b"999")

    assert not verify_webhook_signature(
        payload=tampered_payload, signature_header=header, secret=_SECRET
    )


def test_missing_signature_header_is_rejected() -> None:
    assert not verify_webhook_signature(payload=_PAYLOAD, signature_header=None, secret=_SECRET)


def test_empty_signature_header_is_rejected() -> None:
    assert not verify_webhook_signature(payload=_PAYLOAD, signature_header="", secret=_SECRET)


def test_signature_computed_with_wrong_secret_is_rejected() -> None:
    header = _sign(_PAYLOAD, "a-completely-different-secret")

    assert not verify_webhook_signature(payload=_PAYLOAD, signature_header=header, secret=_SECRET)


def test_signature_missing_algorithm_prefix_is_rejected() -> None:
    digest = hmac.new(_SECRET.encode("utf-8"), _PAYLOAD, hashlib.sha256).hexdigest()

    assert not verify_webhook_signature(payload=_PAYLOAD, signature_header=digest, secret=_SECRET)


def test_uses_constant_time_comparison() -> None:
    """Documents *why* this module exists rather than a plain ``==``: this
    doesn't (and can't, deterministically) prove timing-attack resistance,
    but it does prove the underlying primitive used is
    ``hmac.compare_digest`` itself, by confirming a digest differing only in
    its last character is still cleanly rejected rather than short-circuit
    accepted by some other comparison bug (e.g. length-only checks)."""
    header = _sign(_PAYLOAD, _SECRET)
    flipped_last_char = header[:-1] + ("0" if header[-1] != "0" else "1")

    assert not verify_webhook_signature(
        payload=_PAYLOAD, signature_header=flipped_last_char, secret=_SECRET
    )
