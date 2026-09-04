"""Shared test fixtures for adapter.github.

``test_rsa_private_key_pem`` is a real (freshly generated per test session,
never a committed static blob) RSA private key — genuinely usable by
``adapter.github.auth.sign_app_jwt``'s real RS256 signing path, needed by
every Phase 4 test exercising a real JWT-sign-and-exchange call
(``test_auth.py``, ``test_diff.py``).
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def test_rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")
