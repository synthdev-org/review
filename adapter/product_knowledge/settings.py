"""
adapter.product_knowledge.settings: the product-knowledge bootstrap settings.

Declared once and mixed into both ``WorkerSettings`` and ``AdminAPISettings`` (plan Phase 6,
Assumption 14; same shape as ``adapter.telemetry.sdk.OtelSettingsMixin``).

Each entrypoint still parses its own settings instance at its own
composition root and passes values down; nothing here reads the
environment itself. ``hide_input_in_errors`` is part of the contract: a
validation error on any settings class using this mixin must never echo the
credential map.

The credential map is held raw (``SecretStr``) and parsed on demand by
``credential_map()`` so the two entrypoints can differ in how a malformed
map is handled: the Worker validates it at construction (fail fast, naming
the problem and never the value); the Admin API catches the error and
disables the product-knowledge routes rather than abort boot (an optional
feature must not take down the config/audit UI).
"""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from adapter.product_knowledge.credentials import (
    CredentialMap,
    parse_credential_map,
    parse_endpoint_allowlist,
)

__all__ = ["ProductKnowledgeSettingsMixin"]


class ProductKnowledgeSettingsMixin(BaseSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    # JSON map {"<ref>": {"token": ..., "allowed_hosts": [...]}}; empty or
    # whitespace-only is the same as unset.
    product_knowledge_credentials: SecretStr | None = None
    # Kill switch (restart). Default on: inert without a provider setting.
    product_knowledge_enabled: bool = True
    # Per-call timeout (connect is fixed at 2 s). Worker-only deadline lives
    # on WorkerSettings.
    product_knowledge_call_timeout_seconds: int = 5
    product_knowledge_allow_private_networks: bool = True
    # Comma-separated ``host`` / ``*.suffix`` entries; empty = no allow-list.
    product_knowledge_endpoint_allowlist: str = ""

    @field_validator("product_knowledge_credentials", mode="before")
    @classmethod
    def _blank_credentials_are_unset(cls, value: object) -> object:
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(text, str) and not text.strip():
            return None
        return value

    @field_validator("product_knowledge_call_timeout_seconds")
    @classmethod
    def _call_timeout_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("product_knowledge_call_timeout_seconds must be >= 1")
        return value

    @field_validator("product_knowledge_endpoint_allowlist")
    @classmethod
    def _allowlist_is_valid(cls, value: str) -> str:
        parse_endpoint_allowlist(value)  # raises CredentialMapError (a ValueError)
        return value

    def credential_map(self) -> CredentialMap:
        """
        Parsed credentials; raises ``CredentialMapError`` (value-free) when malformed.
        """
        return parse_credential_map(self.product_knowledge_credentials)

    def endpoint_allowlist(self) -> tuple[str, ...]:
        return parse_endpoint_allowlist(self.product_knowledge_endpoint_allowlist)
