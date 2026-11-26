"""Test-collection setup for the worker test package.

Mirrors `apps/webhook_api/conftest.py`'s own docstring/rationale: `WorkerSettings`
requires a `database_url` field (see that module's
own docstring) — `apps/worker/test_main.py`'s tests call `main()`, which
parses `WorkerSettings()` straight from the process environment, so they
need `REVIEW_DATABASE_URL` set before that happens even though they never
touch a real database (per `adapter.storage.engine.create_storage_engine`/
`create_session_factory`'s own docstrings, both are non-connecting,
synchronous factory calls — safe against an unreachable placeholder DSN).
Tests that actually exercise real claim/dispatch behavior
(`apps/worker/test_dispatch.py`) build their own settings/engine against a
real testcontainers Postgres DSN instead.

Per docs/rules/testing.md's middle tier: `migrated_engine`/`pgvector_container`
are imported from `adapter/storage/conftest.py` (not redefined here) so this
package reuses the same fixture machinery `apps/webhook_api` does, rather
than a second, near-duplicate copy.

`WorkerSettings` also requires two more fields
(`github_app_id`/`github_app_private_key` — see that module's own
docstring): every test in this package that reaches `WorkerSettings()`
parsing now needs both set too, even when the test has nothing to do with
GitHub auth. `TEST_RSA_PRIVATE_KEY_PEM` is a real (test-generated, not
committed as a static blob) RSA private key — genuinely usable by
`adapter.github.auth.sign_app_jwt`'s real RS256 signing path, not a fake
placeholder string like the ones `adapter/github/test_manifest.py` uses for
data that's never actually parsed as a key — so the private-key startup
validation check `apps/worker/main.py` runs on every `main()` call
succeeds by default; tests specifically covering the *malformed*-key
rejection case (`apps/worker/test_settings.py`) override this env var with
a deliberately unusable value instead.
"""

import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401


def _generate_test_rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


TEST_RSA_PRIVATE_KEY_PEM = _generate_test_rsa_private_key_pem()

os.environ.setdefault(
    "REVIEW_DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost:5432/unused"
)
os.environ.setdefault("REVIEW_WORKER_GITHUB_APP_ID", "123456")
os.environ.setdefault("REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY", TEST_RSA_PRIVATE_KEY_PEM)
