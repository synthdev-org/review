"""Cross-checks .env.example against the Settings models.

.env.example must document every bootstrap config value. That's easy to let
drift silently once a Settings field is added/renamed/removed without a matching .env.example
edit — this test catches drift in both directions: a Settings field with no
documented env var, and a documented env var that no longer maps to any
field.

Most fields derive their env var name from the class's own prefix
(``REVIEW_WEBHOOK_API_``/``REVIEW_WORKER_``) plus the field name. One field
(``WebhookAPISettings.database_url``) deliberately overrides that
via ``pydantic.Field(validation_alias=...)`` to reuse the bare
``REVIEW_DATABASE_URL`` variable Alembic/the pgvector startup check already
read directly via ``os.environ`` (see their own module docstrings) — the
same variable name, not a second, prefixed one — so ``_expected_env_vars``
below honors an explicit ``validation_alias`` when a field declares one,
rather than assuming the class prefix always applies.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic_settings import BaseSettings

from apps.webhook_api.settings import WebhookAPISettings
from apps.worker.settings import WorkerSettings

ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / ".env.example"
_ENV_KEY_RE = re.compile(r"^([A-Z0-9_]+)=", re.MULTILINE)


def _env_example_keys() -> set[str]:
    return set(_ENV_KEY_RE.findall(ENV_EXAMPLE_PATH.read_text()))


def _expected_env_vars(settings_cls: type[BaseSettings], prefix: str) -> set[str]:
    expected: set[str] = set()
    for field_name, field_info in settings_cls.model_fields.items():
        alias = field_info.validation_alias
        expected.add(alias if isinstance(alias, str) else f"{prefix}{field_name.upper()}")
    return expected


def test_every_webhook_api_settings_field_is_documented() -> None:
    documented = _env_example_keys()
    expected = _expected_env_vars(WebhookAPISettings, "REVIEW_WEBHOOK_API_")

    missing = expected - documented
    assert not missing, f"WebhookAPISettings fields missing from .env.example: {missing}"


def test_every_worker_settings_field_is_documented() -> None:
    documented = _env_example_keys()
    expected = _expected_env_vars(WorkerSettings, "REVIEW_WORKER_")

    missing = expected - documented
    assert not missing, f"WorkerSettings fields missing from .env.example: {missing}"


def test_no_stale_webhook_api_vars_in_env_example() -> None:
    documented = _env_example_keys()
    expected = _expected_env_vars(WebhookAPISettings, "REVIEW_WEBHOOK_API_")

    stale = {
        key for key in documented if key.startswith("REVIEW_WEBHOOK_API_") and key not in expected
    }
    assert not stale, f".env.example documents REVIEW_WEBHOOK_API_* vars with no field: {stale}"


def test_no_stale_worker_vars_in_env_example() -> None:
    documented = _env_example_keys()
    expected = _expected_env_vars(WorkerSettings, "REVIEW_WORKER_")

    stale = {key for key in documented if key.startswith("REVIEW_WORKER_") and key not in expected}
    assert not stale, f".env.example documents REVIEW_WORKER_* vars with no matching field: {stale}"


def test_database_url_is_documented() -> None:
    # Read directly via os.environ by adapter/storage (see this module's
    # docstring) rather than through a Settings model, so it can't be
    # derived from model_fields like the checks above.
    assert "REVIEW_DATABASE_URL" in _env_example_keys()
