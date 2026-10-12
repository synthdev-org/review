"""tenancy.settings — org/repo/team setting reader/writer logic, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 4 section (Unit E, US-A12,
US-A13, Assumption 2).

Per Assumption 2, this bounded-context extension (not a new package) is
where both the pure scope-precedence resolution function US-A13's second
Given/When/Then requires ("a narrower scope's value overrides a broader
one wherever that setting is read") and the small, closed registry of
known ``OrgRepoTeamSetting.key`` values live — ``tenancy`` already models
``Org``/``Repo``/``Team`` (``tenancy/resolution.py``), so this is a natural
extension of that same bounded context, not a new one.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule: ``resolve_setting`` and ``validate_setting_value`` below are pure
functions over already-loaded data, unit-tested with no I/O, no database,
no FastAPI. ``SettingRepository`` is a ``typing.Protocol`` implemented by
``adapter/storage/tenancy.py``'s ``PostgresSettingRepository`` — defined
here rather than at its consumer (``apps/admin_api/config_routes.py``)
per this phase's own plan section, which names it as a ``tenancy/settings.py``
deliverable alongside the precedence function, not a consumer-defined
boundary the way most other Protocols in this plan are.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class SettingRow(BaseModel):
    """One ``OrgRepoTeamSetting`` row's domain-facing shape.

    ``org_id``/``repo_id``/``team_id`` are all optional — ``None`` narrows
    up to a broader scope, exactly matching that table's own column
    semantics (``repo_id``/``team_id`` ``NULL`` narrows to org-wide or
    platform-wide when ``org_id`` is also ``NULL``). ``value`` is always a
    JSON object (never a bare scalar), matching
    ``adapter/storage/models.py``'s ``OrgRepoTeamSetting.value`` JSONB
    column type.
    """

    model_config = ConfigDict(frozen=True)

    org_id: str | None
    repo_id: str | None
    team_id: str | None
    key: str
    value: dict[str, object]


class SettingStoreError(Exception):
    """Raised by ``SettingRepository`` methods on a real storage-layer
    failure (a malformed id string, a dropped connection mid-write, etc.)
    — never a raw ``sqlalchemy``/``asyncpg``/``ValueError`` exception, per
    ``docs/rules/architecture.md``'s exception-type rule. Mirrors
    ``memory.store.MemoryStoreError``'s exact shape.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"setting store operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class SettingValidationError(Exception):
    """Raised when a write names a setting ``key`` outside the closed
    registry below, or submits a ``value`` that fails that key's own
    schema — US-A13's second Given/When/Then: "the write is rejected (422)
    ... never silently coerced or stored malformed."
    ``apps/admin_api/config_routes.py`` translates this into an HTTP 422.
    """

    def __init__(self, *, key: str, detail: str) -> None:
        super().__init__(f"setting {key!r} rejected: {detail}")
        self.key = key
        self.detail = detail


class ScopeDescriptorError(Exception):
    """Raised by ``parse_scope_descriptor`` for a malformed ``{scope}``
    path segment. ``apps/admin_api/config_routes.py`` translates this into
    an HTTP 400 — a caller-supplied routing mistake, distinct from
    ``SettingValidationError``'s "well-formed scope, bad setting value/key"
    422.
    """

    def __init__(self, *, descriptor: str, detail: str) -> None:
        super().__init__(f"invalid scope descriptor {descriptor!r}: {detail}")
        self.descriptor = descriptor
        self.detail = detail


# --- Closed setting-key registry -------------------------------------------
#
# The registry is closed (an unknown key is always rejected, never silently
# accepted) and each known key's value has a real schema (never a bare,
# unchecked `dict`). The first two keys are the admin round-trip's
# placeholder toggles; `product_knowledge_provider` is the first key with a
# runtime reader (`adapter/product_knowledge/provider_resolver.py`, plan
# `plan-deliver-product-knowledge.md` Phase 6) - whichever further keys a
# phase wires up register their own pydantic model here, same shape.


class MemorySuppressionSetting(BaseModel):
    """Whether Memory-informed finding suppression is enabled at this
    scope — a plausible first real per-org/repo/team toggle, modeled here
    as this registry's first entry."""

    enabled: bool


class FindingsSeverityThresholdSetting(BaseModel):
    """The minimum finding severity this scope wants surfaced."""

    threshold: Literal["low", "medium", "high", "critical"]


PRODUCT_KNOWLEDGE_PROVIDER_KEY = "product_knowledge_provider"

# Keys whose value is meaningful at platform/org/repo scope only: a team is a
# sibling of repos, not an ancestor, so a review job has no team to resolve.
# Writes at team scope are rejected and any orphaned team row is ignored.
TEAM_SCOPE_UNSUPPORTED_KEYS: frozenset[str] = frozenset({PRODUCT_KNOWLEDGE_PROVIDER_KEY})


class ProductKnowledgeProviderSetting(BaseModel):
    """
    Which external product-knowledge provider serves this scope.

    See spec ``spec-product-knowledge.md`` US-PK8. The whole object at the
    narrowest scope wins - no field merging - and ``enabled: false`` at a narrower
    scope opts that scope out of an inherited provider. The token is never
    part of this value: ``credential_ref`` only names an operator-provisioned
    bootstrap credential.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    display_name: str = Field(pattern=r"^[A-Za-z0-9 ._()-]{1,40}$")
    endpoint_url: str
    credential_ref: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    allow_public_repos: bool = False
    issue_key_prefixes: list[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{1,9}$")]] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_url_is_plain_http(cls, value: str) -> str:
        return validate_endpoint_url(value)


def _is_printable_ascii_url(value: str) -> bool:
    """At most 512 chars, ASCII, no whitespace or control characters."""
    return (
        len(value) <= 512 and value.isascii() and not any(c.isspace() or ord(c) < 32 for c in value)
    )


def _has_http_scheme_and_host(parts: SplitResult) -> bool:
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _has_userinfo(parts: SplitResult) -> bool:
    return parts.username is not None or parts.password is not None


def _has_query_or_fragment(parts: SplitResult, value: str) -> bool:
    return bool(parts.query or parts.fragment or "?" in value or "#" in value)


def validate_endpoint_url(value: str) -> str:
    """
    Validate an absolute ``http(s)`` URL of at most 512 chars and return it unchanged.

    It must have a host and no userinfo, fragment or query (a credential or
    secret must never ride the URL). Raises ``ValueError`` (with a fixed message,
    never the value) otherwise.
    """
    if not _is_printable_ascii_url(value):
        raise ValueError("endpoint_url must be a printable ASCII URL of at most 512 characters")
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise ValueError("endpoint_url is not a valid URL") from exc
    if not _has_http_scheme_and_host(parts):
        raise ValueError("endpoint_url must be an absolute http(s) URL with a host")
    if _has_userinfo(parts):
        raise ValueError("endpoint_url must not contain userinfo")
    if _has_query_or_fragment(parts, value):
        raise ValueError("endpoint_url must not contain a query or fragment")
    return value


SETTING_KEY_REGISTRY: dict[str, type[BaseModel]] = {
    "memory_suppression_enabled": MemorySuppressionSetting,
    "findings_severity_threshold": FindingsSeverityThresholdSetting,
    PRODUCT_KNOWLEDGE_PROVIDER_KEY: ProductKnowledgeProviderSetting,
}
"""The small, closed registry Phase 4's own plan section calls for —
``PostgresSettingRepository.upsert`` validates every write against this
before it ever reaches Postgres. A key not present here is always
rejected; it is never silently added or coerced."""


def validate_setting_value(key: str, raw_value: dict[str, object]) -> dict[str, object]:
    """Validates ``raw_value`` against ``key``'s own schema in
    ``SETTING_KEY_REGISTRY``, returning the canonical JSON-safe dict form
    to store.

    Raises ``SettingValidationError`` for an unknown key or a value that
    fails that key's schema — never silently coerced, per US-A13's second
    Given/When/Then.
    """
    model_cls = SETTING_KEY_REGISTRY.get(key)
    if model_cls is None:
        raise SettingValidationError(key=key, detail=f"unknown setting key {key!r}")
    try:
        validated = model_cls.model_validate(raw_value)
    except ValidationError as exc:
        # Field locations and messages only: the offered value is never
        # echoed (a pasted URL may carry userinfo or a token).
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'value'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise SettingValidationError(key=key, detail=detail) from exc
    return validated.model_dump(mode="json")


def resolve_setting(
    key: str,
    rows: Sequence[SettingRow],
    *,
    org_id: str | None = None,
    repo_id: str | None = None,
    team_id: str | None = None,
) -> SettingRow | None:
    """Pure scope-precedence resolution: platform < org < repo/team.

    Given every candidate ``rows`` (any subset already filtered or not —
    this function filters by ``key`` itself) and the scope to resolve for,
    returns the single narrowest-matching row, or ``None`` if nothing at
    any scope sets ``key``. No I/O; no caller-observable side effects.

    Resolution order, most specific first:

    1. The exact requested scope — a repo-scoped row
       (``org_id``, ``repo_id``, ``team_id=None``) if ``repo_id`` was
       given, or a team-scoped row (``org_id``, ``repo_id=None``,
       ``team_id``) if ``team_id`` was given. (Real callers pass at most
       one of ``repo_id``/``team_id`` — a repo and a team are sibling
       scopes under the same org, never nested inside each other, per
       ``adapter/storage/models.py``'s ``OrgRepoTeamSetting`` docstring —
       but if both are given, the repo-scoped row is checked first,
       matching this function's own parameter order, purely for a
       deterministic tie-break rather than a documented precedence
       between the two.)
    2. The org-wide row (``org_id``, ``repo_id=None``, ``team_id=None``),
       if ``org_id`` was given.
    3. The platform-wide row (every column ``None``).
    """
    candidate_scopes: list[tuple[str | None, str | None, str | None]] = []
    if repo_id is not None:
        candidate_scopes.append((org_id, repo_id, None))
    if team_id is not None:
        candidate_scopes.append((org_id, None, team_id))
    if org_id is not None:
        candidate_scopes.append((org_id, None, None))
    candidate_scopes.append((None, None, None))

    by_scope = {(row.org_id, row.repo_id, row.team_id): row for row in rows if row.key == key}
    for scope in candidate_scopes:
        row = by_scope.get(scope)
        if row is not None:
            return row
    return None


class EffectiveSetting(BaseModel):
    """
    One key's resolved value at a node plus the scope descriptor it was inherited from.

    The descriptor is one of ``"platform"``, ``"org:<id>"``, ``"repo:<org>:<repo>"``.
    """

    model_config = ConfigDict(frozen=True)

    value: dict[str, object]
    source: str


def effective_settings(
    rows: Sequence[SettingRow],
    *,
    org_id: str | None,
    repo_id: str | None,
    team_id: str | None,
) -> dict[str, EffectiveSetting]:
    """
    Every registry key's effective value at one node, via ``resolve_setting``.

    ``resolve_setting`` is the single precedence implementation - the Admin UI
    never re-derives it. Keys outside the registry (orphaned rows after a
    code revert) are not resolved; keys in ``TEAM_SCOPE_UNSUPPORTED_KEYS``
    ignore team-scoped rows. A resolved row whose value fails its key's schema
    is omitted (unconfigured), never shown as effective.
    """
    effective: dict[str, EffectiveSetting] = {}
    for key in SETTING_KEY_REGISTRY:
        row = resolve_setting(
            key,
            rows,
            org_id=org_id,
            repo_id=repo_id,
            team_id=None if key in TEAM_SCOPE_UNSUPPORTED_KEYS else team_id,
        )
        if row is not None:
            try:
                validate_setting_value(key, row.value)
            except SettingValidationError:
                # A stored value its own schema rejects is unusable: consumers
                # treat it as not configured (no fall-through to a parent), so
                # it must not be presented as the effective value.
                continue
            effective[key] = EffectiveSetting(
                value=row.value,
                source=format_scope_descriptor(
                    org_id=row.org_id, repo_id=row.repo_id, team_id=row.team_id
                ),
            )
    return effective


def format_scope_descriptor(*, org_id: str | None, repo_id: str | None, team_id: str | None) -> str:
    """The inverse of ``parse_scope_descriptor`` — used by
    ``apps/admin_api/config_routes.py``'s ``GET /api/v1/config`` response
    to hand the frontend a ready-to-use ``{scope}`` value for a later
    ``PUT /api/v1/config/{scope}/{key}`` call against the same scope.
    """
    if org_id is None:
        return "platform"
    if repo_id is not None:
        return f"repo:{org_id}:{repo_id}"
    if team_id is not None:
        return f"team:{org_id}:{team_id}"
    return f"org:{org_id}"


def parse_scope_descriptor(descriptor: str) -> tuple[str | None, str | None, str | None]:
    """Parses a ``PUT /api/v1/config/{scope}/{key}`` ``{scope}`` path
    segment into ``(org_id, repo_id, team_id)``.

    A compact, colon-separated descriptor (rather than a nested path) is
    this phase's own implementation-shape choice, since FastAPI's default
    path-parameter matching doesn't split a single ``{scope}`` segment on
    ``/`` — not specified by the plan itself. Accepted forms:

    - ``"platform"`` -> ``(None, None, None)``
    - ``"org:<org_id>"`` -> ``(org_id, None, None)``
    - ``"repo:<org_id>:<repo_id>"`` -> ``(org_id, repo_id, None)``
    - ``"team:<org_id>:<team_id>"`` -> ``(org_id, None, team_id)``

    The owning org's id is required alongside a repo/team id (rather than
    looked up server-side from the repo/team row) so this function stays a
    pure parse with no I/O, and so ``resolve_setting``'s own org-fallback
    step always has the right org id to fall back to. Raises
    ``ScopeDescriptorError`` for anything else, including an empty id
    segment.
    """
    parts = descriptor.split(":")
    if parts == ["platform"]:
        return None, None, None
    if len(parts) == 2 and parts[0] == "org" and parts[1]:
        return parts[1], None, None
    if len(parts) == 3 and parts[0] == "repo" and parts[1] and parts[2]:
        return parts[1], parts[2], None
    if len(parts) == 3 and parts[0] == "team" and parts[1] and parts[2]:
        return parts[1], None, parts[2]
    raise ScopeDescriptorError(
        descriptor=descriptor,
        detail=(
            "expected 'platform', 'org:<org_id>', 'repo:<org_id>:<repo_id>', "
            "or 'team:<org_id>:<team_id>'"
        ),
    )


class ScopeSettings(BaseModel):
    """One org/repo/team's own identity plus the literal
    ``OrgRepoTeamSetting`` rows set directly at that scope (never
    resolved/inherited — US-A12's own "its current OrgRepoTeamSetting
    key/value entries" wording) — the per-node shape
    ``GET /api/v1/config``'s response tree is built from.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    scope: str
    settings: dict[str, dict[str, object]]
    using_platform_defaults: bool
    # Resolved (inherited) values per registry key, computed server-side by
    # ``effective_settings``; ``settings`` above stays the literal own rows.
    effective: dict[str, EffectiveSetting] = Field(default_factory=dict)


class OrgConfig(ScopeSettings):
    """One org's own node in the config tree, plus its repos and teams."""

    model_config = ConfigDict(frozen=True)

    repos: tuple[ScopeSettings, ...]
    teams: tuple[ScopeSettings, ...]


class ConfigTree(BaseModel):
    """``GET /api/v1/config``'s full response shape — US-A12's "org → repo
    → team hierarchy," plus the platform-wide settings every scope falls
    back to.
    """

    model_config = ConfigDict(frozen=True)

    platform_settings: dict[str, dict[str, object]]
    platform_effective: dict[str, EffectiveSetting] = Field(default_factory=dict)
    orgs: tuple[OrgConfig, ...]


class SettingRepository(Protocol):
    """Storage boundary for ``OrgRepoTeamSetting`` rows, implemented by
    ``adapter/storage/tenancy.py``'s ``PostgresSettingRepository``. Two
    methods, matching this phase's own plan section wording ("get-by-scope,
    upsert"): ``get_config_tree`` reads every org/repo/team's own literal
    settings in one call (US-A12), and ``upsert`` writes one setting at one
    exact scope (US-A13), validated against ``SETTING_KEY_REGISTRY`` before
    it ever reaches Postgres.
    """

    async def get_config_tree(self) -> ConfigTree: ...

    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        """
        Return the platform, org and repo rows (never team rows) for one key.

        This is the narrow keyed read a runtime resolver feeds ``resolve_setting``,
        instead of loading the whole config tree.
        """
        ...

    async def upsert(
        self,
        *,
        org_id: str | None,
        repo_id: str | None,
        team_id: str | None,
        key: str,
        value: dict[str, object],
    ) -> SettingRow: ...
