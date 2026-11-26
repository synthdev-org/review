"""Unit tests for AdminAPISettings' OIDC validation (US-A24)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from apps.admin_api.settings import AdminAPISettings


def _settings(**overrides: object) -> AdminAPISettings:
    base: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr("postgresql+asyncpg://u:p@h/d"),
        "admin_bootstrap_username": "a",
        "admin_bootstrap_password": SecretStr("a-long-enough-password"),
        "admin_csrf_secret": SecretStr("s"),
        "_env_file": None,
    }
    return AdminAPISettings(**{**base, **overrides})  # type: ignore[arg-type]


def test_oidc_is_off_by_default() -> None:
    assert _settings().oidc_provider == "none"


def test_authentik_with_all_fields_is_valid() -> None:
    _settings(
        oidc_provider="authentik",
        oidc_client_id="c",
        oidc_redirect_uri="https://r/cb",
        oidc_authentik_base_url="https://idp",
        oidc_authentik_app_slug="review",
    )


@pytest.mark.parametrize(
    ("provider", "extra", "missing"),
    [
        ("generic", {"oidc_client_id": "c", "oidc_redirect_uri": "u"}, "oidc_issuer"),
        ("authentik", {"oidc_client_id": "c", "oidc_redirect_uri": "u"}, "oidc_authentik_app_slug"),
        ("generic", {"oidc_issuer": "i", "oidc_redirect_uri": "u"}, "oidc_client_id"),
        ("generic", {"oidc_issuer": "i", "oidc_client_id": "c"}, "oidc_redirect_uri"),
    ],
)
def test_enabled_provider_with_a_missing_field_fails_at_startup(
    provider: str, extra: dict[str, str], missing: str
) -> None:
    with pytest.raises(ValidationError, match=missing):
        _settings(oidc_provider=provider, **extra)
