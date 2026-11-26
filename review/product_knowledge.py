"""
The product-knowledge contract's wire models and limits (review.product_knowledge).

US-PK16, per docs/specs/spec-product-knowledge.md's "The contract (version 1)".

**These pydantic models are the source of truth for the wire contract.** The
JSON Schemas committed under ``docs/reference/product-knowledge-contract/v1/``
are generated from them (``adapter/product_knowledge/test_contract_schemas.py``
fails on drift); the schemas, not this file, are what a provider implementer
reads.

Request models forbid unknown fields (review only ever sends arguments a
provider's own ``inputSchema`` declares). Response models ignore unknown
fields — the additive-only rule: a provider may add optional fields within a
major version and a v1 consumer must not break on them. Field constraints
here are the *strict* (conformance) bounds; the tolerant consumer behavior
(truncate rather than drop, ``source_type`` -> ``other``) is normalization
logic that operates on the raw payload and arrives with the domain logic below.

The domain logic section adds the pure logic on top of the wire models: the
consumer-defined Protocols, ``ProviderOutcome``, the A1 exposure gate,
deterministic query formulation, tolerant result normalization with the
**single** URL sanitizer, and per-section assembly. Everything here is pure
(no I/O, no infra imports; ADR-0005's pure-logic exemption, extended for
this module).
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Protocol
from urllib.parse import SplitResult, quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "CONTRACT_VERSION",
    "FETCH_CONTENT_MAX_CHARS",
    "FETCH_TOP_N",
    "MAX_RESPONSE_BYTES",
    "QUERY_MAX_CHARS",
    "SECTION_MAX_BYTES",
    "SNIPPET_MAX_CHARS",
    "SUPPORTED_MAJORS",
    "TITLE_MAX_CHARS",
    "TOOL_CONTRACT",
    "TOOL_FETCH",
    "TOOL_RELATED",
    "TOOL_SEARCH",
    "TOP_K",
    "URL_MAX_CHARS",
    "ContractDeclaration",
    "ContractRequest",
    "EffectiveProvider",
    "EffectiveProviderResolver",
    "FetchRequest",
    "FetchResponse",
    "NegotiationResult",
    "NormalizedResults",
    "ProductKnowledgeError",
    "ProductKnowledgeItem",
    "ProductKnowledgeProvider",
    "ProductKnowledgeSection",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderErrorDetail",
    "ProviderItem",
    "ProviderOpener",
    "ProviderOutcome",
    "RelatedRequest",
    "SearchItem",
    "SearchRequest",
    "SearchResponse",
    "assemble_product_knowledge",
    "formulate_query",
    "normalize_fetch",
    "normalize_results",
    "product_knowledge_prompt_payload",
    "sanitize_url",
    "should_query",
    "url_display",
]

# --- Tool names (exact, case-sensitive) -----------------------------------

TOOL_CONTRACT: Final = "product_knowledge_contract"
TOOL_SEARCH: Final = "product_knowledge_search"
TOOL_FETCH: Final = "product_knowledge_fetch"
TOOL_RELATED: Final = "product_knowledge_related"

# --- Versioning ------------------------------------------------------------

CONTRACT_VERSION: Final = "1"
# Majors this build speaks (ADR-0013 decision 4: N and N-1; only 1 exists).
SUPPORTED_MAJORS: Final = frozenset({CONTRACT_VERSION})

# --- Limits review enforces regardless of provider behavior ----------------

MAX_RESPONSE_BYTES: Final = 256 * 1024
TOP_K: Final = 5  # items review consumes from one response
SNIPPET_MAX_CHARS: Final = 1000
FETCH_CONTENT_MAX_CHARS: Final = 4000
TITLE_MAX_CHARS: Final = 200
URL_MAX_CHARS: Final = 2048
SECTION_MAX_BYTES: Final = 12 * 1024  # serialized ceiling, independent of the diff
QUERY_MAX_CHARS: Final = 512
FETCH_TOP_N: Final = 2  # items whose content is replaced by a fetch

_ID_MAX_CHARS: Final = 512
_ERROR_MESSAGE_MAX_CHARS: Final = 500
_MAX_TOP_K: Final = 20
_FETCH_MIN_CHARS: Final = 256
_FETCH_MAX_CHARS: Final = 16000

_MAJOR_PATTERN: Final = r"^[0-9]+$"
_SOURCE_TYPE_PATTERN: Final = r"^[a-z][a-z0-9_.-]{0,31}$"

_Major = Annotated[str, StringConstraints(pattern=_MAJOR_PATTERN)]
_SourceType = Annotated[str, StringConstraints(pattern=_SOURCE_TYPE_PATTERN)]
_ItemId = Annotated[str, StringConstraints(min_length=1, max_length=_ID_MAX_CHARS)]

_REQUEST_CONFIG: Final = ConfigDict(extra="forbid")
_RESPONSE_CONFIG: Final = ConfigDict(extra="ignore")


# --- product_knowledge_contract --------------------------------------------


class ContractRequest(BaseModel):
    """``product_knowledge_contract`` takes no arguments."""

    model_config = _REQUEST_CONFIG


class ContractDeclaration(BaseModel):
    """Output of ``product_knowledge_contract``: the majors a provider speaks."""

    model_config = _RESPONSE_CONFIG

    supported_versions: Annotated[list[_Major], Field(min_length=1)]


# --- product_knowledge_search / _related -----------------------------------


class SearchRequest(BaseModel):
    model_config = _REQUEST_CONFIG

    contract_version: _Major
    query: Annotated[str, StringConstraints(min_length=1, max_length=QUERY_MAX_CHARS)]
    top_k: Annotated[int, Field(ge=1, le=_MAX_TOP_K)] = TOP_K


class SearchItem(BaseModel):
    """One ranked result. ``title`` is required (F1): a citation needs it."""

    model_config = _RESPONSE_CONFIG

    id: _ItemId
    title: Annotated[str, StringConstraints(min_length=1, max_length=TITLE_MAX_CHARS)]
    snippet: Annotated[str, StringConstraints(max_length=SNIPPET_MAX_CHARS)]
    source_type: _SourceType
    url: Annotated[str, StringConstraints(max_length=URL_MAX_CHARS)] | None = None
    score: Annotated[float, Field(ge=0, le=1)] | None = None
    updated_at: datetime | None = None


class SearchResponse(BaseModel):
    """``results`` is ordered best-first; array order is the ranking."""

    model_config = _RESPONSE_CONFIG

    results: Annotated[list[SearchItem], Field(max_length=_MAX_TOP_K)]


class RelatedRequest(BaseModel):
    """Optional tool; review v1 never calls it, but conformance tests it."""

    model_config = _REQUEST_CONFIG

    contract_version: _Major
    id: _ItemId
    top_k: Annotated[int, Field(ge=1, le=_MAX_TOP_K)] = TOP_K


# --- product_knowledge_fetch -------------------------------------------------


class FetchRequest(BaseModel):
    model_config = _REQUEST_CONFIG

    contract_version: _Major
    id: _ItemId
    max_chars: Annotated[int, Field(ge=_FETCH_MIN_CHARS, le=_FETCH_MAX_CHARS)] = (
        FETCH_CONTENT_MAX_CHARS
    )


class FetchResponse(BaseModel):
    model_config = _RESPONSE_CONFIG

    id: str
    title: Annotated[str, StringConstraints(max_length=TITLE_MAX_CHARS)]
    content: str
    content_format: Literal["text", "markdown"] = "text"
    source_type: _SourceType
    url: Annotated[str, StringConstraints(max_length=URL_MAX_CHARS)] | None = None
    updated_at: datetime | None = None
    truncated: bool = False


# --- Error shape -------------------------------------------------------------

ProviderErrorCode = Literal[
    "invalid_argument",
    "not_found",
    "unauthorized",
    "forbidden",
    "rate_limited",
    "unsupported_version",
    "unavailable",
    "internal",
]


class ProviderErrorDetail(BaseModel):
    model_config = _RESPONSE_CONFIG

    code: ProviderErrorCode
    message: Annotated[str, StringConstraints(max_length=_ERROR_MESSAGE_MAX_CHARS)]
    retry_after_seconds: Annotated[int, Field(ge=0)] | None = None


class ProviderError(BaseModel):
    """
    ``structuredContent`` of a tool result with ``isError: true``.

    Unknown ``code`` values fail validation here (the set is closed for
    providers); a consumer classifies an unparseable error as
    ``provider_error`` itself.
    """

    model_config = _RESPONSE_CONFIG

    error: ProviderErrorDetail


# --- Negotiation -------------------------------------------------------------


class NegotiationResult(BaseModel):
    """
    Outcome of ``product_knowledge_contract`` negotiation.

    Adapter-neutral so test-connection needs no adapter-specific entry point.

    ``chosen_version`` is the highest major both sides support, or ``None``
    when there is no overlap (treated as "no provider"). ``supported_versions``
    holds only values that matched the major pattern.
    """

    model_config = ConfigDict(frozen=True)

    chosen_version: _Major | None
    supported_versions: list[_Major]
    tools_present: list[str]
    # ``chosen_version`` is a major the review build still supports but has
    # marked deprecated (US-PK18); test-connection reports it, it still works.
    deprecated: bool = False


# =============================================================================
# Domain logic
# =============================================================================


class ProviderOutcome(StrEnum):
    """
    Closed set of per-job provider-branch outcomes (the ``outcome`` metric label).

    ``credential_unavailable``, ``blocked_egress``, ``unsupported_version``,
    ``auth_failed`` and ``redaction_disabled`` are config-class values: warned,
    not just counted.
    """

    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    NO_QUERY = "no_query"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    REDACTION_DISABLED = "redaction_disabled"
    SKIPPED_PUBLIC_REPO = "skipped_public_repo"
    SKIPPED_FORK = "skipped_fork"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    BLOCKED_EGRESS = "blocked_egress"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    UNSUPPORTED_VERSION = "unsupported_version"
    DEPRECATED_VERSION = "deprecated_version"
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    PROVIDER_ERROR = "provider_error"


class ProductKnowledgeError(Exception):
    """
    Raised by a ``ProductKnowledgeProvider``/``ProviderOpener`` for any failure.

    Carries only the closed ``outcome`` — never provider text, so nothing
    sensitive can ride an exception message or a log line.
    """

    def __init__(self, outcome: ProviderOutcome, status_code: int | None = None) -> None:
        """Record the closed ``outcome`` and the optional HTTP ``status_code``."""
        super().__init__(outcome.value)
        self.outcome = outcome
        # The provider's HTTP status when one was seen (a number, never text).
        self.status_code = status_code


class EffectiveProvider(BaseModel):
    """
    The resolved per-repo provider setting, in the shape the review domain consumes.

    The ``tenancy``-backed resolver maps into it.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    display_name: str
    endpoint_url: str
    credential_ref: str | None = None
    allow_public_repos: bool = False
    issue_key_prefixes: tuple[str, ...] = ()


class ProductKnowledgeProvider(Protocol):
    """
    One negotiated connection to a provider endpoint.

    ``search``/``fetch`` return the provider's raw ``structuredContent``
    (tolerant normalization is this module's job) and raise
    ``ProductKnowledgeError`` on any failure. ``related`` is deliberately
    absent (v1 never calls it).
    """

    async def negotiate(self) -> NegotiationResult:
        """Negotiate protocol capabilities with the provider."""

    async def search(self, query: str) -> object:
        """Search the provider for ``query``."""

    async def fetch(self, item_id: str) -> object:
        """Fetch one item by id."""


class ProviderOpener(Protocol):
    """
    Builds a provider client for an endpoint.

    The credential is named by ``credential_ref`` only — the token never enters
    this package; the composition-root implementation resolves it (and
    re-checks the host binding, raising ``credential_unavailable``).
    """

    def __call__(self, endpoint_url: str, credential_ref: str | None) -> ProductKnowledgeProvider:
        """Open a provider client for ``endpoint_url``."""


class EffectiveProviderResolver(Protocol):
    """
    Resolves the effective provider setting for one repo (``None`` ⇒ not configured).

    Implementations fail open; they never raise.
    """

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        """Return the effective provider setting, or ``None``."""


def should_query(
    repo_private: bool, pr_is_fork: bool | None, setting: EffectiveProvider
) -> ProviderOutcome | None:
    """
    The A1 exposure gate. ``None`` ⇒ proceed; otherwise the skip outcome.

    Public repos and fork PRs are skipped unless the setting opts in with
    ``allow_public_repos``. ``pr_is_fork is None`` (an old payload) is
    treated as a fork: fail closed.
    """
    if setting.allow_public_repos:
        return None
    if not repo_private:
        return ProviderOutcome.SKIPPED_PUBLIC_REPO
    if pr_is_fork is None or pr_is_fork:
        return ProviderOutcome.SKIPPED_FORK
    return None


def formulate_query(title: str | None, issue_keys: Sequence[str]) -> str | None:
    """
    Build the query: normalized title followed by the issue keys, at most 512 chars.

    Whole keys only; a key already present in the title is not repeated.
    ``None`` ⇒ nothing to ask (``no_query``). Deterministic, no I/O; redaction
    is the caller's job (it is async/subprocess-backed). The whitespace
    normalization here is defence in depth: callers may pass a title that
    redaction/masking has already touched, and the 512-char bound must hold
    regardless.
    """
    parts: list[str] = []
    length = 0
    normalized = " ".join(title.split()) if title else ""
    if normalized:
        normalized = normalized[:QUERY_MAX_CHARS]
        parts.append(normalized)
        length = len(normalized)
    for key in issue_keys:
        key = key.strip()
        if not key or length + 1 + len(key) > QUERY_MAX_CHARS:
            continue
        if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", normalized):
            continue
        parts.append(key)
        length += 1 + len(key)
    return " ".join(parts) or None


# --- Sanitizers ---------------------------------------------------------------

# Cs (lone surrogates) cannot be UTF-8 encoded, so they would crash URL quoting
# and the JSON body of a posted comment; Co (private use) is never meaningful.
_STRIP_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_HOST_RE: Final = re.compile(r"^(?:[A-Za-z0-9._-]+|[0-9A-Fa-f:.]+)$")
# Percent-encoding everything outside this set also encodes ``( ) [ ] < > "``
# and space, so a URL can never terminate a Markdown ``[t](url)`` destination.
_URL_SAFE: Final = "/%:@!$&'*+,;=~-._"


def _clean_text(text: str, *, keep_newlines: bool = False) -> str:
    r"""
    Replace control/format/surrogate/line-separator characters with a space.

    Includes ``\n``, ``\r``, U+2028/2029 and bidi overrides.
    """
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch) in _STRIP_CATEGORIES and not (keep_newlines and ch in "\n\t"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _clean_line(text: str, limit: int) -> str:
    return " ".join(_clean_text(text).split())[:limit]


def _parse_http_url(url: str) -> tuple[SplitResult, str, int | None] | None:
    """
    Return ``(parts, hostname, port)`` for an ``http``/``https`` URL with a host.

    The URL must carry no userinfo; ``None`` otherwise (incl. a malformed port).
    """
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    return parts, host, port


def _ascii_host(host: str) -> str | None:
    """The lower-cased IDNA/ASCII form of ``host``, ``None`` if unusable."""
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return host if _HOST_RE.fullmatch(host) else None


def _rebuild_url(parts: SplitResult, host: str, port: int | None) -> str:
    """Canonical, percent-encoded URL from already-validated components."""
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    result = f"{parts.scheme.lower()}://{netloc}{quote(parts.path, safe=_URL_SAFE)}"
    if parts.query:
        result += "?" + quote(parts.query, safe=_URL_SAFE + "?")
    if parts.fragment:
        result += "#" + quote(parts.fragment, safe=_URL_SAFE + "?")
    return result


def sanitize_url(url: object) -> str | None:
    """
    The single URL sanitizer (normalization and citation both use it).

    Returns a canonical, percent-encoded ``http``/``https`` URL, or ``None``
    when it is not a string, is too long, contains control characters, uses
    another scheme, carries userinfo, or has an unusable host/port.
    """
    if not isinstance(url, str) or not url or len(url) > URL_MAX_CHARS:
        return None
    if any(unicodedata.category(ch) in _STRIP_CATEGORIES for ch in url):
        return None
    parsed = _parse_http_url(url)
    if parsed is None:
        return None
    parts, raw_host, port = parsed
    host = _ascii_host(raw_host)
    if host is None:
        return None
    result = _rebuild_url(parts, host, port)
    return result if len(result) <= URL_MAX_CHARS else None


def url_display(url: str | None) -> tuple[str, tuple[str, ...]] | None:
    """
    Return ``(host, flags)`` for a sanitized URL, ``None`` when it fails the sanitizer.

    Flags name cleartext ``http`` and IP-literal hosts.
    """
    safe = sanitize_url(url)
    if safe is None:
        return None
    parts = urlsplit(safe)
    host = parts.hostname or ""
    flags: list[str] = []
    if parts.scheme == "http":
        flags.append("cleartext http")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        flags.append("IP address")
    return host, tuple(flags)


# --- Normalization --------------------------------------------------------------


class ProviderItem(BaseModel):
    """One validated, sanitized provider result (no review-assigned id yet)."""

    model_config = ConfigDict(frozen=True)

    provider_item_id: str
    title: str
    source_type: str
    url: str | None = None
    updated_at: datetime | None = None
    content: str


class NormalizedResults(BaseModel):
    """``outcome`` is one of ``ok``/``empty``/``partial``/``malformed``."""

    model_config = ConfigDict(frozen=True)

    items: tuple[ProviderItem, ...] = ()
    outcome: ProviderOutcome


def _parse_updated_at(raw_updated: object) -> datetime | None:
    """ISO-8601 string to ``datetime``; anything else (or malformed) ``None``."""
    if not isinstance(raw_updated, str):
        return None
    try:
        return datetime.fromisoformat(raw_updated)
    except ValueError:
        return None


def _normalize_item(raw: object) -> ProviderItem | None:
    if not isinstance(raw, Mapping):
        return None
    item_id, title, snippet, source_type = (
        raw.get("id"),
        raw.get("title"),
        raw.get("snippet"),
        raw.get("source_type"),
    )
    if not (
        isinstance(item_id, str)
        and 0 < len(item_id) <= _ID_MAX_CHARS
        and _clean_text(item_id) == item_id
        and isinstance(title, str)
        and isinstance(snippet, str)
        and isinstance(source_type, str)
    ):
        return None
    clean_title = _clean_line(title, TITLE_MAX_CHARS)
    if not clean_title:
        return None
    if not re.fullmatch(_SOURCE_TYPE_PATTERN, source_type):
        source_type = "other"
    return ProviderItem(
        provider_item_id=item_id,
        title=clean_title,
        source_type=source_type,
        url=sanitize_url(raw.get("url")),
        updated_at=_parse_updated_at(raw.get("updated_at")),
        content=_clean_text(snippet, keep_newlines=True)[:SNIPPET_MAX_CHARS],
    )


def normalize_results(raw: object) -> NormalizedResults:
    """
    Tolerant consumer of a ``product_knowledge_search`` payload.

    Only the first ``TOP_K`` results are considered; invalid items are dropped
    (some ⇒ ``partial``, all ⇒ ``malformed``); over-long fields are truncated.
    """
    results = raw.get("results") if isinstance(raw, Mapping) else None
    if not isinstance(results, list):
        return NormalizedResults(outcome=ProviderOutcome.MALFORMED)
    considered = results[:TOP_K]
    if not considered:
        return NormalizedResults(outcome=ProviderOutcome.EMPTY)
    items = [item for item in map(_normalize_item, considered) if item is not None]
    if not items:
        return NormalizedResults(outcome=ProviderOutcome.MALFORMED)
    outcome = ProviderOutcome.OK if len(items) == len(considered) else ProviderOutcome.PARTIAL
    return NormalizedResults(items=tuple(items), outcome=outcome)


def normalize_fetch(raw: object, fallback: ProviderItem) -> ProviderItem:
    """
    Replace ``fallback``'s snippet with the fetched content (capped).

    Any unusable fetch payload keeps the snippet. Identity fields (title, url,
    source type) always come from the search result.
    """
    content = raw.get("content") if isinstance(raw, Mapping) else None
    if not isinstance(content, str):
        return fallback
    cleaned = _clean_text(content, keep_newlines=True)[:FETCH_CONTENT_MAX_CHARS]
    if not cleaned.strip():
        return fallback
    return fallback.model_copy(update={"content": cleaned})


# --- Assembly -----------------------------------------------------------------


class ProductKnowledgeItem(ProviderItem):
    """
    A provider item with its bundle-wide integer id.

    Continues after the last code item id, so ``context_ref_ids`` needs no
    schema change.
    """

    id: int


class ProductKnowledgeSection(BaseModel):
    """The provider's labeled section of a ``ContextBundle``."""

    model_config = ConfigDict(frozen=True)

    label: str
    items: tuple[ProductKnowledgeItem, ...] = ()


def product_knowledge_prompt_payload(item: ProductKnowledgeItem) -> dict[str, object]:
    """
    Return what the LLM sees per item: title, source type, updated_at, content.

    Never the provider's own id or the URL.
    """
    return {
        "id": item.id,
        "title": item.title,
        "source_type": item.source_type,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "content": item.content,
    }


def _payload_size(item: ProductKnowledgeItem) -> int:
    # ensure_ascii=False: the ceiling is a true UTF-8 byte count (and matches
    # what ``render_product_knowledge_section`` serializes), so non-Latin
    # content is not charged 6 bytes per character for ``\\uXXXX`` escapes.
    payload = product_knowledge_prompt_payload(item)
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _truncate_to_fit(item: ProductKnowledgeItem, budget: int) -> ProductKnowledgeItem:
    """
    Return the longest content prefix of ``item`` whose payload fits ``budget`` bytes.

    Binary search; size is monotonic in prefix length.
    """
    lo, hi = 0, len(item.content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _payload_size(item.model_copy(update={"content": item.content[:mid]})) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return item.model_copy(update={"content": item.content[:lo]})


def assemble_product_knowledge(
    items: Sequence[ProviderItem], *, label: str, first_id: int
) -> ProductKnowledgeSection | None:
    """
    Assemble best-first ``items`` into a section of at most ``TOP_K`` items.

    Content is capped (4,000 chars for the top ``FETCH_TOP_N``, 1,000 for the
    rest) and the serialized total held to ``SECTION_MAX_BYTES`` by dropping
    the lowest-ranked first; if the best item alone is still over, its content
    is truncated to fit rather than the item being lost. Independent of the
    diff-proportional code budget. ``None`` only for empty input (title is
    bounded well under the ceiling, so a non-empty input never empties).
    """
    built: list[ProductKnowledgeItem] = []
    for rank, item in enumerate(items[:TOP_K]):
        cap = FETCH_CONTENT_MAX_CHARS if rank < FETCH_TOP_N else SNIPPET_MAX_CHARS
        built.append(
            ProductKnowledgeItem(
                **item.model_copy(update={"content": item.content[:cap]}).model_dump(),
                id=first_id + rank,
            )
        )
    while len(built) > 1 and sum(_payload_size(i) for i in built) > SECTION_MAX_BYTES:
        built.pop()
    if built and _payload_size(built[0]) > SECTION_MAX_BYTES:
        built[0] = _truncate_to_fit(built[0], SECTION_MAX_BYTES)
    if not built:
        return None
    return ProductKnowledgeSection(label=label, items=tuple(built))
