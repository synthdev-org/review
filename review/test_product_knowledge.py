"""Unit tests for the Phase 3 pure domain logic in review.product_knowledge
(US-PK1-3, PK6, PK7, PK20), per ADR-0005's pure-no-I/O exemption. The
Protocols are exercised through fakes only; nothing here touches a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from review.product_knowledge import (
    FETCH_CONTENT_MAX_CHARS,
    FETCH_TOP_N,
    MAX_RESPONSE_BYTES,
    QUERY_MAX_CHARS,
    SECTION_MAX_BYTES,
    SNIPPET_MAX_CHARS,
    TITLE_MAX_CHARS,
    TOP_K,
    URL_MAX_CHARS,
    EffectiveProvider,
    NegotiationResult,
    ProductKnowledgeError,
    ProductKnowledgeItem,
    ProductKnowledgeProvider,
    ProviderItem,
    ProviderOutcome,
    _ascii_host,
    _parse_http_url,
    _parse_updated_at,
    _rebuild_url,
    assemble_product_knowledge,
    formulate_query,
    normalize_fetch,
    normalize_results,
    product_knowledge_prompt_payload,
    sanitize_url,
    should_query,
    url_display,
)


def _setting(**overrides: object) -> EffectiveProvider:
    fields: dict[str, object] = {
        "display_name": "Acme Wiki",
        "endpoint_url": "https://wiki.example.com/mcp",
    }
    fields.update(overrides)
    return EffectiveProvider.model_validate(fields)


def _raw_item(index: int = 0, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": f"doc-{index}",
        "title": f"Doc {index}",
        "snippet": f"snippet {index}",
        "source_type": "confluence",
        "url": f"https://wiki.example.com/doc/{index}",
        "score": 0.5,
        "updated_at": "2026-09-01T10:00:00Z",
    }
    item.update(overrides)
    return item


def _pk_item(item: ProviderItem) -> ProductKnowledgeItem:
    return ProductKnowledgeItem(**item.model_dump(), id=7)


def _provider_item(index: int = 0, *, content: str = "c") -> ProviderItem:
    return ProviderItem(
        provider_item_id=f"doc-{index}",
        title=f"Doc {index}",
        source_type="confluence",
        url=f"https://wiki.example.com/doc/{index}",
        content=content,
    )


# --- should_query: the A1 exposure gate -----------------------------------------


@pytest.mark.parametrize(
    ("private", "fork", "allow", "expected"),
    [
        (True, False, False, None),
        (False, False, False, ProviderOutcome.SKIPPED_PUBLIC_REPO),
        (True, True, False, ProviderOutcome.SKIPPED_FORK),
        (True, None, False, ProviderOutcome.SKIPPED_FORK),  # old payload: fail closed
        (False, True, False, ProviderOutcome.SKIPPED_PUBLIC_REPO),
        (False, False, True, None),
        (True, True, True, None),
        (False, None, True, None),
    ],
)
def test_should_query_gate_matrix(
    private: bool, fork: bool | None, allow: bool, expected: ProviderOutcome | None
) -> None:
    assert should_query(private, fork, _setting(allow_public_repos=allow)) is expected


# --- formulate_query --------------------------------------------------------------


def test_formulate_query_is_title_then_keys_and_deterministic() -> None:
    assert formulate_query("Fix  race in  cache", ("PROJ-9", "#41")) == (
        "Fix race in cache PROJ-9 #41"
    )
    assert formulate_query("Fix  race in  cache", ("PROJ-9", "#41")) == formulate_query(
        "Fix race in cache", ("PROJ-9", "#41")
    )


@pytest.mark.parametrize(("title", "keys"), [(None, ()), ("", ()), ("   ", ())])
def test_formulate_query_nothing_to_ask_is_none(title: str | None, keys: tuple[str, ...]) -> None:
    assert formulate_query(title, keys) is None


def test_formulate_query_keys_only_and_title_only() -> None:
    assert formulate_query(None, ("PROJ-1",)) == "PROJ-1"
    assert formulate_query("Hello", ()) == "Hello"


def test_formulate_query_never_exceeds_512_and_keeps_whole_keys() -> None:
    keys = tuple(f"PROJ-{i}" for i in range(10))
    query = formulate_query("t" * 300, keys)
    assert query is not None and len(query) <= 512
    long = formulate_query("t" * 500, ("PROJ-1", "PROJ-2"))
    assert long is not None and len(long) <= 512
    assert formulate_query("x" * 600, ()) == "x" * 512


def test_formulate_query_does_not_repeat_a_key_already_in_the_title() -> None:
    assert formulate_query("PROJ-1 Add refunds", ("PROJ-1",)) == "PROJ-1 Add refunds"
    assert formulate_query("Fix (PROJ-1).", ("PROJ-1", "PROJ-2")) == "Fix (PROJ-1). PROJ-2"
    # A longer key containing the shorter one is a different key.
    assert formulate_query("See PROJ-10", ("PROJ-1",)) == "See PROJ-10 PROJ-1"


# --- closed sets, limits, wire models ---------------------------------------------


def test_provider_outcome_is_the_closed_set_of_19_metric_labels() -> None:
    assert {o.value for o in ProviderOutcome} == {
        "ok",
        "empty",
        "partial",
        "no_query",
        "not_configured",
        "disabled",
        "redaction_disabled",
        "skipped_public_repo",
        "skipped_fork",
        "timeout",
        "unreachable",
        "auth_failed",
        "blocked_egress",
        "credential_unavailable",
        "unsupported_version",
        "deprecated_version",
        "malformed",
        "oversized",
        "provider_error",
    }
    assert len(ProviderOutcome) == 19


def test_limit_constants_are_the_documented_values() -> None:
    assert MAX_RESPONSE_BYTES == 262144
    assert TOP_K == 5
    assert SNIPPET_MAX_CHARS == 1000
    assert FETCH_CONTENT_MAX_CHARS == 4000
    assert TITLE_MAX_CHARS == 200
    assert URL_MAX_CHARS == 2048
    assert SECTION_MAX_BYTES == 12288
    assert QUERY_MAX_CHARS == 512
    assert FETCH_TOP_N == 2


def test_negotiation_result_constructs_and_is_frozen() -> None:
    result = NegotiationResult(
        chosen_version="1", supported_versions=["1"], tools_present=["product_knowledge_search"]
    )
    assert result.deprecated is False
    assert (
        NegotiationResult(
            chosen_version=None, supported_versions=[], tools_present=[]
        ).chosen_version
        is None
    )
    with pytest.raises(ValidationError):
        result.chosen_version = "2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        NegotiationResult(chosen_version="v1", supported_versions=[], tools_present=[])


def test_contract_readme_states_versioning_policy_and_required_title() -> None:
    readme = (
        Path(__file__).resolve().parents[1]
        / "docs/reference/product-knowledge-contract/v1/README.md"
    ).read_text()
    assert "## Versioning policy" in readme
    assert "additive only" in readme
    assert "current and the previous major (N, N-1)" in readme
    assert "at least 12 months" in readme
    assert "**`title` is required**" in readme


# --- sanitize_url / url_display ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html;base64,AAAA",
        "ftp://wiki.example.com/x",
        "//wiki.example.com/x",
        "https://user:pw@wiki.example.com/x",
        "https://user@wiki.example.com/x",
        "https://wiki.example.com/x\n[evil](http://e)",
        "https://wiki.example.com/\u2028x",
        "https://wiki.example.com/\u202ex",
        "https://wiki.example.com:99999/x",
        "https://",
        "",
        "https://wiki.example.com/" + "a" * 2100,
        "https://[fe80::1%25eth0]/x",
        "http://a.com/\ud800",
        None,
        42,
    ],
)
def test_sanitize_url_rejects_hostile_inputs(url: object) -> None:
    assert sanitize_url(url) is None


def test_sanitize_url_canonicalizes_and_encodes_markdown_breakers() -> None:
    out = sanitize_url("HTTPS://Wiki.Example.com:8443/a b/(x)?q=[1]&r=<2>#frag(1)")
    assert out == "https://wiki.example.com:8443/a%20b/%28x%29?q=%5B1%5D&r=%3C2%3E#frag%281%29"
    assert all(c not in out for c in " ()[]<>")


def test_sanitize_url_is_idempotent() -> None:
    once = sanitize_url("https://wiki.example.com/a b/(x)?q=%41&z=1 2")
    assert once is not None
    assert sanitize_url(once) == once


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Wiki.example.com:8443/x", ("wiki.example.com", 8443)),
        ("http://wiki.example.com/x", ("wiki.example.com", None)),
        ("HTTP://[::1]/x", ("::1", None)),
        ("ftp://wiki.example.com/x", None),
        ("//wiki.example.com/x", None),
        ("https:///x", None),
        ("https://u@wiki.example.com/x", None),
        ("https://wiki.example.com:99999/x", None),
    ],
)
def test_parse_http_url_accepts_only_hosted_http_without_userinfo(
    url: str, expected: tuple[str, int | None] | None
) -> None:
    parsed = _parse_http_url(url)
    assert (parsed if parsed is None else (parsed[1], parsed[2])) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("Wiki.Example.com", "wiki.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("::1", "::1"),
        ("10.0.0.1", "10.0.0.1"),
        ("a b.example", None),
        ("a_b!.example", None),
        ("x" * 70 + ".example", None),
    ],
)
def test_ascii_host_normalizes_or_rejects(host: str, expected: str | None) -> None:
    assert _ascii_host(host) == expected


@pytest.mark.parametrize(
    ("url", "host", "port", "expected"),
    [
        ("HTTPS://x/a b?q=[1]#f(1)", "x", None, "https://x/a%20b?q=%5B1%5D#f%281%29"),
        ("http://x:80/", "x", 80, "http://x:80/"),
        ("http://[::1]:9/p", "::1", 9, "http://[::1]:9/p"),
        ("http://x", "x", None, "http://x"),
    ],
)
def test_rebuild_url_is_canonical(url: str, host: str, port: int | None, expected: str) -> None:
    parts = _parse_http_url(url)
    assert parts is not None
    assert _rebuild_url(parts[0], host, port) == expected


@pytest.mark.parametrize(
    ("raw", "expected_iso"),
    [
        ("2026-01-02T03:04:05+00:00", "2026-01-02T03:04:05+00:00"),
        ("2026-01-02", "2026-01-02T00:00:00"),
        ("not-a-date", None),
        ("", None),
        (None, None),
        (20260102, None),
    ],
)
def test_parse_updated_at_is_tolerant(raw: object, expected_iso: str | None) -> None:
    parsed = _parse_updated_at(raw)
    assert (parsed.isoformat() if parsed is not None else None) == expected_iso


def test_url_display_flags_cleartext_and_ip_literals() -> None:
    assert url_display("https://wiki.example.com/x") == ("wiki.example.com", ())
    assert url_display("http://wiki.example.com/x") == ("wiki.example.com", ("cleartext http",))
    assert url_display("https://10.0.0.5/x") == ("10.0.0.5", ("IP address",))
    assert url_display("http://[::1]:8080/x") == ("::1", ("cleartext http", "IP address"))
    assert url_display("javascript:x") is None
    assert url_display(None) is None


# --- normalize_results ------------------------------------------------------------


def test_normalize_all_valid_is_ok_and_preserves_order() -> None:
    result = normalize_results({"results": [_raw_item(0), _raw_item(1)]})
    assert result.outcome is ProviderOutcome.OK
    assert [i.provider_item_id for i in result.items] == ["doc-0", "doc-1"]
    assert result.items[0].updated_at is not None


def test_normalize_empty_results() -> None:
    assert normalize_results({"results": []}).outcome is ProviderOutcome.EMPTY


@pytest.mark.parametrize(
    "raw",
    [None, "x", [], {}, {"results": "nope"}, {"results": [{"id": "1"}, 7, None]}],
)
def test_normalize_malformed(raw: object) -> None:
    result = normalize_results(raw)
    assert result.outcome is ProviderOutcome.MALFORMED
    assert result.items == ()


def test_normalize_some_invalid_is_partial() -> None:
    raw = {
        "results": [
            _raw_item(0),
            {"id": "", "title": "t", "snippet": "s", "source_type": "jira"},
            _raw_item(2),
            _raw_item(3, title=5),
            _raw_item(4, title="   "),
        ]
    }
    result = normalize_results(raw)
    assert result.outcome is ProviderOutcome.PARTIAL
    assert [i.provider_item_id for i in result.items] == ["doc-0", "doc-2"]


def test_normalize_only_considers_first_top_k() -> None:
    raw = {"results": [_raw_item(i) for i in range(TOP_K)] + [{"junk": True}] * 3}
    result = normalize_results(raw)
    assert result.outcome is ProviderOutcome.OK
    assert len(result.items) == TOP_K


def test_normalize_truncates_and_keeps_item_on_bad_url_and_source_type() -> None:
    raw = {
        "results": [
            _raw_item(
                0,
                title="T" * 500,
                snippet="s" * 5000,
                source_type="Not A Token!",
                url="javascript:alert(1)",
                updated_at="not-a-date",
            )
        ]
    }
    (item,) = normalize_results(raw).items
    assert len(item.title) == TITLE_MAX_CHARS
    assert len(item.content) == SNIPPET_MAX_CHARS
    assert item.source_type == "other"
    assert item.url is None
    assert item.updated_at is None


def test_normalize_strips_control_characters() -> None:
    raw = {
        "results": [
            _raw_item(
                0,
                title="Line1\nLine2\r\u2028\u2029\x00Done\u202e",
                snippet="a\x00b\u2028c\nkeep\tme",
            )
        ]
    }
    (item,) = normalize_results(raw).items
    assert item.title == "Line1 Line2 Done"
    assert "\x00" not in item.content and "\u2028" not in item.content
    assert item.content == "a b c\nkeep\tme"


def test_snippet_and_fetched_content_keep_newlines_but_title_id_url_never_do() -> None:
    """Deliberate asymmetry: multi-line bodies stay readable (and are safe,
    because the prompt/comment serialise them as JSON), while single-line
    identity fields can never carry a newline."""
    raw = {
        "results": [
            _raw_item(0, title="T\nTwo", snippet="line1\nline2", url="https://w.example.com/a\nb"),
            _raw_item(1, id="doc\n1"),
        ]
    }
    result = normalize_results(raw)
    assert result.outcome is ProviderOutcome.PARTIAL  # the newline id is invalid
    (item,) = result.items
    assert item.title == "T Two"
    assert item.url is None  # a newline in a URL rejects it outright
    assert item.content == "line1\nline2"

    fetched = normalize_fetch({"content": "para1\n\npara2"}, item)
    assert fetched.content == "para1\n\npara2"

    wire = json.dumps(product_knowledge_prompt_payload(_pk_item(fetched)), ensure_ascii=False)
    assert "\n" not in wire  # json-serialised: the newline is the two-char escape
    assert "para1\\n\\npara2" in wire


def test_normalize_lone_surrogates_never_raise_or_survive() -> None:
    raw = {
        "results": [
            _raw_item(0, title="t\ud800", snippet="s\udc00x", url="http://a.com/\ud800"),
            _raw_item(1, id="bad\ud800id"),
        ]
    }
    result = normalize_results(raw)
    assert result.outcome is ProviderOutcome.PARTIAL
    (item,) = result.items
    assert item.url is None
    for text in (item.title, item.content):
        text.encode("utf-8")  # raises on a surviving surrogate


def test_normalize_overlong_id_is_invalid() -> None:
    assert (
        normalize_results({"results": [_raw_item(0, id="x" * 513)]}).outcome
        is ProviderOutcome.MALFORMED
    )


# --- normalize_fetch ----------------------------------------------------------------


def test_fetch_replaces_snippet_with_capped_content_keeping_identity() -> None:
    fallback = _provider_item(0, content="snip")
    out = normalize_fetch(
        {"content": "z" * 9000, "title": "Evil", "url": "https://evil.example/"}, fallback
    )
    assert len(out.content) == FETCH_CONTENT_MAX_CHARS
    assert out.title == fallback.title and out.url == fallback.url


@pytest.mark.parametrize("raw", [None, {}, {"content": 5}, {"content": "  \n"}, "x"])
def test_failed_fetch_keeps_the_snippet(raw: object) -> None:
    fallback = _provider_item(0, content="snip")
    assert normalize_fetch(raw, fallback) == fallback


# --- assemble_product_knowledge ------------------------------------------------------


def test_assemble_assigns_ids_after_code_ids() -> None:
    section = assemble_product_knowledge(
        [_provider_item(0), _provider_item(1)], label="Acme Wiki", first_id=7
    )
    assert section is not None
    assert section.label == "Acme Wiki"
    assert [i.id for i in section.items] == [7, 8]


def test_assemble_empty_is_none() -> None:
    assert assemble_product_knowledge([], label="x", first_id=0) is None


def test_assemble_caps_items_and_per_rank_content() -> None:
    items = [_provider_item(i, content="x" * 4000) for i in range(8)]
    section = assemble_product_knowledge(items, label="W", first_id=0)
    assert section is not None
    assert len(section.items) == TOP_K
    lengths = [len(i.content) for i in section.items]
    assert lengths == [FETCH_CONTENT_MAX_CHARS] * 2 + [SNIPPET_MAX_CHARS] * 3


def test_assemble_ceiling_drops_lowest_ranked_first() -> None:
    # "é" is 2 UTF-8 bytes: 5 items of 1500/1000 chars exceed 12 KiB, so the
    # lowest-ranked is dropped and the survivors are a best-first prefix.
    items = [_provider_item(i, content="é" * 1500) for i in range(5)]
    section = assemble_product_knowledge(items, label="W", first_id=0)
    assert section is not None
    assert [i.provider_item_id for i in section.items] == [f"doc-{n}" for n in range(4)]
    total = sum(
        len(json.dumps(product_knowledge_prompt_payload(i), ensure_ascii=False).encode())
        for i in section.items
    )
    assert total <= SECTION_MAX_BYTES


def test_assemble_non_ascii_top_item_is_truncated_not_dropped() -> None:
    # 4000 four-byte chars = 16 KB of UTF-8: over the ceiling on its own.
    items = [_provider_item(0, content="\U0001f600" * 4000)]
    section = assemble_product_knowledge(items, label="W", first_id=0)
    assert section is not None
    (item,) = section.items
    assert 0 < len(item.content) < 4000
    size = len(json.dumps(product_knowledge_prompt_payload(item), ensure_ascii=False).encode())
    assert size <= SECTION_MAX_BYTES
    assert size > SECTION_MAX_BYTES - 4  # maximal prefix: one more char would not fit


def test_assemble_cyrillic_fetch_with_snippets_keeps_top_item() -> None:
    items = [_provider_item(0, content="\u0434" * 4000)] + [
        _provider_item(i, content="\u0434" * 200) for i in range(1, 4)
    ]
    section = assemble_product_knowledge(items, label="W", first_id=0)
    assert section is not None
    assert section.items[0].provider_item_id == "doc-0"
    assert len(section.items[0].content) == 4000


# --- Protocols through fakes (consumer-defined, no network) ---------------------------


class _FakeProvider:
    def __init__(self, search_payload: object, fetch_payload: object) -> None:
        self._search = search_payload
        self._fetch = fetch_payload
        self.fetched: list[str] = []

    async def negotiate(self) -> NegotiationResult:
        return NegotiationResult(
            chosen_version="1", supported_versions=["1"], tools_present=["product_knowledge_search"]
        )

    async def search(self, query: str) -> object:
        assert query
        return self._search

    async def fetch(self, item_id: str) -> object:
        self.fetched.append(item_id)
        if item_id == "doc-1":
            raise ProductKnowledgeError(ProviderOutcome.TIMEOUT)
        return self._fetch


class _FakeOpener:
    def __init__(self, provider: ProductKnowledgeProvider) -> None:
        self.provider = provider
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, endpoint_url: str, credential_ref: str | None) -> ProductKnowledgeProvider:
        self.calls.append((endpoint_url, credential_ref))
        return self.provider


class _FakeResolver:
    def __init__(self, setting: EffectiveProvider | None) -> None:
        self._setting = setting

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        return self._setting


@pytest.mark.asyncio
async def test_protocols_compose_end_to_end_with_fakes() -> None:
    provider = _FakeProvider(
        {"results": [_raw_item(0), _raw_item(1), _raw_item(2)]}, {"content": "full body"}
    )
    opener = _FakeOpener(provider)
    resolver = _FakeResolver(_setting(credential_ref="wiki"))

    setting = await resolver.resolve("org", "repo")
    assert setting is not None
    assert should_query(True, False, setting) is None
    client = opener(setting.endpoint_url, setting.credential_ref)
    assert opener.calls == [("https://wiki.example.com/mcp", "wiki")]
    assert (await client.negotiate()).chosen_version == "1"

    query = formulate_query("PROJ-1 Add refunds", ("PROJ-1",))
    assert query is not None
    result = normalize_results(await client.search(query))
    items = list(result.items)
    for index in range(2):  # top-2 fetch; a failed fetch keeps the snippet
        try:
            items[index] = normalize_fetch(
                await client.fetch(items[index].provider_item_id), items[index]
            )
        except ProductKnowledgeError as err:
            assert err.outcome is ProviderOutcome.TIMEOUT
    section = assemble_product_knowledge(items, label=setting.display_name, first_id=3)

    assert section is not None
    assert [i.id for i in section.items] == [3, 4, 5]
    assert section.items[0].content == "full body"
    assert section.items[1].content == "snippet 1"
