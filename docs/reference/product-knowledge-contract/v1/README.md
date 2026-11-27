# Product-knowledge contract, version 1

The wire contract a provider implements so `review` can fetch non-code
product knowledge (tickets, wiki pages) at review time. Normative prose:
[`spec-product-knowledge.md`](../../../specs/spec-product-knowledge.md) ("The
contract (version 1)"); decisions:
[ADR-0013](../../../adr/0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md).

**These JSON Schemas are generated** from the pydantic models in
[`review/product_knowledge.py`](../../../../review/product_knowledge.py) and a
test (`adapter/product_knowledge/test_contract_schemas.py`) fails if any file
here differs from what the models generate. Do not edit them by hand; change
the models and regenerate with
`PK_UPDATE_SCHEMAS=1 pytest adapter/product_knowledge/test_contract_schemas.py`.

## Tools

Transport is MCP over Streamable HTTP. Tool names are exact and
case-sensitive. Every call after negotiation carries `contract_version`.

| Tool | Required | Input schema | Output schema |
| --- | --- | --- | --- |
| `product_knowledge_contract` | yes | [`contract.input.json`](contract.input.json) | [`contract.output.json`](contract.output.json) |
| `product_knowledge_search` | yes | [`search.input.json`](search.input.json) | [`search.output.json`](search.output.json) |
| `product_knowledge_fetch` | yes | [`fetch.input.json`](fetch.input.json) | [`fetch.output.json`](fetch.output.json) |
| `product_knowledge_related` | optional | [`related.input.json`](related.input.json) | [`related.output.json`](related.output.json) |

Tool failures use MCP `isError: true` with the shape in
[`error.json`](error.json). The `code` set is closed in v1.

Valid and invalid examples per schema live under [`examples/valid/`](examples/valid/)
and [`examples/invalid/`](examples/invalid/), named `<tool>.<input|output>.<case>.json`
(`error.<case>.json` for the error shape). Every valid example is validated against
its schema in CI and every invalid one must fail.

## Notes for implementers

- **`title` is required** on every `search` result and on `fetch` output (it
  is what review's deterministic citation shows the PR author). ADR-0003's
  earlier `{id, snippet, source_type, url, score}` item shape did not carry
  it; it was added before any provider existed, so it is not a version break.
- `url` and `score` are optional and nullable; `updated_at` is optional.
- `results` is ordered best-first; array order is the ranking. `score` is
  advisory and never thresholded on.
- Providers must not send more than 256 KiB in a response. Review truncates
  over-long fields rather than dropping the item; the conformance suite fails
  a provider that exceeds a limit.
- Consumers ignore unknown response fields; review never sends request
  arguments a provider's own `inputSchema` does not declare.

## Provider-implementer guide

Everything a third party needs to make a system (a wiki, a ticket tracker, a
RAG product) usable by `review`. No review-side code changes per provider:
review is configured with only an endpoint URL, an optional credential name
and a display name.

### 1. Serve the four tools over MCP Streamable HTTP

| Tool | Arguments | Returns |
| --- | --- | --- |
| `product_knowledge_contract` | none | `{"supported_versions": ["1"]}` (non-empty list of integer-major strings) |
| `product_knowledge_search` | `contract_version`, `query` (<= 512 chars), optional `top_k` (1-20; review sends 5) | `{"results": [SearchItem, ...]}` best-first |
| `product_knowledge_fetch` | `contract_version`, `id` (an id a search returned), optional `max_chars` | `{id, title, content, content_format, source_type, url?, updated_at?, truncated}` |
| `product_knowledge_related` *(optional)* | `contract_version`, `id` | same shape as `search` |

Return results as MCP `structuredContent` matching the committed schemas.
Both MCP protocol eras the official SDK speaks (legacy `initialize` and
`server/discover`) are accepted by review's client; a provider on the official
`mcp` server SDKs gets both for free. Declare your tool `inputSchema`s
accurately: review sends only arguments they declare.

### 2. Field semantics

- `id`: opaque, stable, provider-scoped; `fetch` must accept any id `search`
  returned. `title` (<= 200 chars) is what the PR author sees in the citation.
- `snippet` (<= 1000 chars) is **untrusted data** to review: it goes into the
  LLM prompt inside a framed, JSON-serialized section and is never shown in
  the PR comment. Fetched `content` is cut to 4,000 characters.
- `source_type`: `^[a-z][a-z0-9_.-]{0,31}$` (`wiki`, `ticket`, ...); other
  values are mapped to `other`.
- `url` must be an absolute `http(s)` URL without userinfo; anything else is
  dropped (the item is kept). The citation shows the URL's host.
- `score` (0-1) is advisory; ordering is the ranking.
- `updated_at`: RFC 3339, optional.
- Errors: MCP `isError: true` with `{"code", "message"}` from the closed set in
  [`error.json`](error.json) (`invalid_argument`, `not_found`, `unauthorized`,
  `forbidden`, `rate_limited`, `unsupported_version`, `unavailable`, `internal`). review never echoes the message into prompts, logs or
  the UI.

### 3. Authentication

Static bearer token: review sends `Authorization: Bearer <token>` on every
request, only to the hosts the operator bound to that credential. Return `401`
for a missing/invalid token. review does not do OAuth or client-credentials,
follows no redirects (serve the final URL) and does not honor proxy
environment variables. Hosts in private ranges are reachable by default;
loopback, link-local and cloud-metadata addresses never are.

### 4. Prove conformance

```bash
PK_TOKEN=... python -m adapter.product_knowledge.conformance --pk-url https://kb.example.com/mcp
```

The suite is the same one CI runs against the in-repo fake: contract tool and
declared version, schema validity of every response, ordering and limits
(strict: a snippet over 1000 characters fails), `title` on every item, `fetch`
of a returned id, `not_found` for an unknown id, `unsupported_version` for an
unknown `contract_version`, `top_k` honored, response size, auth rejection and
`related` shape when present. Pass `--pk-query <term>` with a term your
endpoint knows if the default query returns nothing. Fix every failing check before pointing review
at the endpoint; the operator's first-line diagnostic is the Admin UI's
test-connection button, which runs the same negotiation.

### 5. What you should assume about exposure

Review sends your endpoint **only the secret-redacted PR title and linked issue
keys**, from the Worker, once per review job. Whatever you return can end up in
an LLM prompt and, if an attacker-influenced PR gets the model to quote it, in a
PR comment. Therefore:

- scope the token to the least data review needs (a read-only service account
  limited to the spaces/projects the repos concern);
- do not return content the *PR author's audience* must not see; operators
  should leave `allow_public_repos` off, and review skips public repos and fork
  PRs by default;
- treat `query` as untrusted input (it derives from user-controlled text).

## Versioning policy

(Copied from ADR-0013 decision 4; that ADR is authoritative.)

- A contract version is an integer major written as a string (`"1"`). Within
  a major, evolution is additive only: new optional response fields, new
  optional tools, new open-string values. Anything else is a new major.
- A provider declares the majors it implements via the mandatory
  zero-argument `product_knowledge_contract` tool, returning
  `{"supported_versions": ["1"]}`.
- Review supports the current and the previous major (N, N-1) and selects the
  highest major both sides support. No overlap means "no provider configured",
  never a failed review.
- A major is marked deprecated when its successor ships and is not removed for
  at least 12 months after that release, announced in release notes, with a
  warning in the Admin UI test-connection result and a
  `deprecated_contract_version` log field in the meantime.
- Existing provider configurations never pin a version, so a new major rolling
  out changes nothing for them.
