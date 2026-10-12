"""adapter.product_knowledge.credentials — the operator-provisioned credential
map (ADR-0013 decision 2, plan Phase 6, Assumption 14), parsed once and
shared by the Worker and Admin API composition roots.

The map is bootstrap config: one ``SecretStr`` holding
``{"<ref>": {"token": "...", "allowed_hosts": ["host", ...]}}``. A token is
only ever usable against a host in its own ``allowed_hosts``; that binding is
checked at write time, at test-connection time and at job time, always with
the one canonicaliser in ``egress.py``.

**No error from this module ever contains a token or any part of the raw
map**: pydantic's ``input_value`` is suppressed and messages are built from
field locations and fixed phrases only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from adapter.product_knowledge.egress import (
    endpoint_origin,
    host_matches_allowlist,
    parse_allow_entry,
)
from adapter.product_knowledge.transport import ProviderCredential

__all__ = [
    "BindingProblem",
    "CredentialMap",
    "CredentialMapError",
    "parse_credential_map",
    "parse_endpoint_allowlist",
    "validate_credential_ref",
]

REF_PATTERN: Final = re.compile(r"^[a-z0-9_-]{1,64}$")

BindingProblem = Literal[
    "unknown_credential_ref", "credential_host_not_bound", "unauthenticated_host_not_allowed"
]


class CredentialMapError(ValueError):
    """The credential map (or allow-list) is malformed. ``str(error)`` names
    the problem and never a value."""


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    token: SecretStr
    allowed_hosts: list[str]


_FIELDS: Final = ("token", "allowed_hosts")


def validate_credential_ref(ref: str) -> bool:
    return REF_PATTERN.fullmatch(ref) is not None


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise CredentialMapError("credential map contains a duplicate ref")
        seen[key] = value
    return seen


def _bound_host(entry: str) -> str | None:
    """Canonical ``host`` / ``host:port`` for an ``allowed_hosts`` entry, else
    ``None``. Wildcards, schemes and paths are never bindable: a token bound
    to ``*`` or ``*.example.com`` is a token an Admin can aim anywhere. A
    ``:port`` is kept: it pins the token to that port exactly."""
    parsed = parse_allow_entry(entry)
    if parsed is None or parsed[1]:
        return None
    host, _, port = parsed
    if port is None:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class CredentialMap:
    """Parsed, validated credentials. Empty ⇒ no credential configured."""

    _by_ref: Mapping[str, ProviderCredential]

    @classmethod
    def empty(cls) -> CredentialMap:
        return cls({})

    def __bool__(self) -> bool:
        return bool(self._by_ref)

    def get(self, ref: str) -> ProviderCredential | None:
        return self._by_ref.get(ref)

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_ref))

    def allowed_hosts(self, ref: str) -> tuple[str, ...]:
        credential = self._by_ref.get(ref)
        return credential.allowed_hosts if credential is not None else ()

    def all_allowed_hosts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(h for c in self._by_ref.values() for h in c.allowed_hosts))

    def any_binds(self, host: str, port: int | None = None) -> bool:
        """True if some credential is bound to ``host`` (test-connection's
        "is this a target the operator provisioned" check)."""
        return any(
            host_matches_allowlist(host, c.allowed_hosts, port) for c in self._by_ref.values()
        )

    def binding_problem(
        self,
        ref: str | None,
        endpoint_url: str,
        endpoint_allowlist: Iterable[str] = (),
    ) -> BindingProblem | None:
        """The write-time / test-time / job-time check.

        With a ``ref``: it must exist and be bound to the endpoint's host
        (and port, when the entry carries one). With ``ref=None``
        (unauthenticated provider) the endpoint must match the operator
        ``endpoint_allowlist`` or some credential's ``allowed_hosts`` — the
        same rule test-connection applies — so an Admin cannot aim the
        Worker at an arbitrary in-cluster host.
        """
        origin = endpoint_origin(endpoint_url)
        if ref is None:
            if origin is None or not (
                host_matches_allowlist(origin[0], endpoint_allowlist, origin[1])
                or self.any_binds(*origin)
            ):
                return "unauthenticated_host_not_allowed"
            return None
        credential = self._by_ref.get(ref)
        if credential is None:
            return "unknown_credential_ref"
        if origin is None or not host_matches_allowlist(
            origin[0], credential.allowed_hosts, origin[1]
        ):
            return "credential_host_not_bound"
        return None


def parse_credential_map(raw: SecretStr | str | None) -> CredentialMap:
    """Parse and validate the bootstrap credential map.

    ``None`` and empty/whitespace-only input are *unset* (``.env.example``,
    Compose ``${VAR:-}`` and Helm ``value: ""`` all produce the latter).
    Raises ``CredentialMapError`` with a value-free message otherwise.
    """
    text = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
    if text is None or not text.strip():
        return CredentialMap.empty()
    try:
        decoded = json.loads(text, object_pairs_hook=_reject_duplicates)
    except CredentialMapError:
        raise
    except (ValueError, RecursionError):
        # Not ``from`` the decode error: its text can echo input fragments.
        raise CredentialMapError("credential map is not valid JSON") from None
    if not isinstance(decoded, dict) or not decoded:
        raise CredentialMapError("credential map must be a non-empty JSON object")

    parsed: dict[str, ProviderCredential] = {}
    for index, (ref, body) in enumerate(decoded.items(), start=1):
        if not validate_credential_ref(ref):
            raise CredentialMapError(
                f"credential #{index} has an invalid ref (expected [a-z0-9_-]{{1,64}})"
            )
        entry: _Entry | None = None
        bad_fields: list[str] = []
        try:
            entry = _Entry.model_validate(body)
        except ValidationError as exc:
            # Only the two known field names are ever echoed: an unexpected
            # key's *name* could itself be a pasted token.
            bad_fields = sorted(
                {
                    str(e["loc"][0]) if e["loc"] and e["loc"][0] in _FIELDS else "unexpected field"
                    for e in exc.errors()
                }
            )
        if entry is None:
            # Raised outside the ``except`` block: no ``__context__`` chain,
            # so the pydantic error (whose loc may be a token-named key)
            # can never ride a traceback.
            raise CredentialMapError(
                f"credential {ref!r} is invalid: check {', '.join(bad_fields)} "
                "(expected {token, allowed_hosts})"
            )
        if not entry.token.get_secret_value().strip():
            raise CredentialMapError(f"credential {ref!r} has an empty token")
        if not entry.allowed_hosts:
            raise CredentialMapError(
                f"credential {ref!r} has no allowed_hosts (a token must be bound to hosts)"
            )
        hosts: list[str] = []
        for host_entry in entry.allowed_hosts:
            bound = _bound_host(host_entry)
            if bound is None:
                raise CredentialMapError(
                    f"credential {ref!r} has an invalid allowed_hosts entry "
                    "(an exact host or host:port, without scheme, path or wildcard)"
                )
            hosts.append(bound)
        parsed[ref] = ProviderCredential(
            ref=ref, token=entry.token, allowed_hosts=tuple(dict.fromkeys(hosts))
        )
    return CredentialMap(parsed)


def parse_endpoint_allowlist(raw: str) -> tuple[str, ...]:
    """Comma-separated operator allow-list (``host``, ``*.suffix``, each with
    an optional ``:port``). Empty ⇒ no allow-list. Raises
    ``CredentialMapError`` on an unusable entry (these are not secrets, so
    the entry may be named). An entry with a port matches that port exactly;
    one without matches any port (weaker — prefer ``host:port``)."""
    entries: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if parse_allow_entry(item) is None:
            raise CredentialMapError(
                f"endpoint allow-list entry {item!r} is not a host, *.suffix or either with :port"
            )
        entries.append(item.lower())
    return tuple(dict.fromkeys(entries))
