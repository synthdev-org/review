# Plan: Phase 7 — Optional external product-knowledge augmentation

**Delivered: Phases 1-9 are implemented.** Amendments A1–A5 were confirmed by
the owner at the recommended defaults on 2026-10-11 and applied to
`spec-product-knowledge.md` (new US-PK20; US-PK2/PK8/PK10 extended; security
table corrected) and ADR-0013 decision 3. **Merge gates still outstanding:**
ADR-0013 is still `PROPOSED` (acceptance gates merging Phase 4's `mcp`
dependency); **Workstream K** (knowhow registering the contract tools) is
outstanding. **Security review (Units D and F) was run 2026-10-12 as a
dedicated read-only pass:** no critical or high findings; three medium
(unauthenticated provider had no host constraint; credential binding was
host-only; `issue_key_prefixes` did not constrain free-text title queries) and
eight low findings, all fixed with tests except L6 (Viewers can read the stored
`endpoint_url` path) and L5 (no internal-CA option; cleartext token delivery to
private addresses), which are documented in `docs/reference/operations.md`.
Deployment note: an unauthenticated provider (`credential_ref` null) now needs
an operator allow-list entry (or a matching credential host). Phase 8's E2E (`tests/e2e/test_admin_ui.py`)
is API plus served-bundle level: the harness has no browser, so the panel's
interaction is covered by vitest only, not a browser-driven step.
Sequences [`docs/specs/spec-product-knowledge.md`](../specs/spec-product-knowledge.md)
— [`docs/roadmap.md`](../roadmap.md)'s Phase 7 — and
[ADR-0013](../adr/0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)
into buildable, independently-testable phases, following the same
phase/why-first/tests/observability-and-rollback/acceptance shape
[`plan-deliver-memory.md`](plan-deliver-memory.md) established as this
repo's `docs/plans/` convention (that file is the *format* template only).
Built against [`docs/rules/architecture.md`](../rules/architecture.md)
(verification tag `WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md)
(verification tag `RQPTVE`).

The spec decides the product/architecture questions in this phase. **This
plan does not re-litigate them; it orders the work, names the real files
each phase extends (checked against the working tree on 2026-10-11), and
records implementation-shape choices as Assumptions.** Where an eight-
dimension `plan-review` of the first draft surfaced a gap that is a
*decision* rather than sequencing (a security posture the spec did not
state, a contract/ADR tightening), it is recorded as an **Amendment** for the
owner rather than silently patched into a phase.

**Preconditions (gates, not phases):**

- **ADR-0013 `PROPOSED` → `ACCEPTED`** on the owner's sign-off. Phases 1–3
  may start on a `PROPOSED` ADR; **Phase 4 adds the `mcp` dependency and
  must not merge before acceptance.**
- **Amendments A1–A5 confirmed** (next section). A1 gates Phases 2, 3 and 7;
  A2 gates Phase 6; A3 gates Phase 4; A4 gates Phase 6; A5 gates Phase 7.

This is additive work on running code (`review/context_builder.py`,
`review/context_bundle.py`, `review/citation.py`, `tenancy/settings.py`,
`apps/admin_api/config_routes.py`, `apps/worker/main.py`), not greenfield.

## Amendments required before the gated phases (owner decisions)

Each has a **recommended default** (what the plan assumes if confirmed as-is)
and the spec/ADR text it changes. None is invented scope: each closes a gap
the review found between what the spec/ADR *claims* and what its phases
*deliver*.

| # | Gap found | Recommended default | Changes |
| --- | --- | --- | --- |
| **A1** | **The spec's "lethal trifecta" claim is false.** The security section says no tools/write path means "two of three legs are structurally absent". But the finding text is posted as a PR comment — a world-readable exfiltration channel on a public repo — and the PR title/diff/body are attacker-controlled by any GitHub user (incl. forks). A PR saying "quote the ticket in your finding" can leak provider content; "non-verbatim" is a prompt request, not a control. Separately, issue keys are extracted from the **PR body/title/branch**, so an unprivileged author can name `HR-77` and have the operator's token fetch it (top-2 fetch, 4,000 chars) into the prompt: targeted retrieval of arbitrary provider documents. | (a) Provider branch runs **only for private repos**, unless the provider setting sets `allow_public_repos: true` (explicit, per-scope opt-in, shown in the Admin UI disclosure); (b) **skip fork PRs** (head repo ≠ base repo) unless the same opt-in is set; (c) optional `issue_key_prefixes` allow-list on the provider setting — when set, only matching keys are queried/fetched; (d) correct the spec/plan wording: the residual is "attacker-influenced prompt can exfiltrate provider content into a PR comment" and is controlled by (a)–(c), not by absence of tools. | `spec-product-knowledge.md` (Security table, US-PK2, setting model, Out of scope), Phase 2 payload fields (`repo_private` already carried; add `pr_is_fork`) |
| **A2** | **test-connection is an SSRF/port-scan oracle and a token-delivery path.** The spec/plan don't say the write-time `unknown_credential_ref` / `credential_host_not_bound` checks run on the test request body; with null `credential_ref` and private ranges allowed, status classes (`unreachable`/`no_contract`/`malformed`/`auth_failed`) plus `latency_ms`/`tools_present`/provider-supplied `supported_versions` let a scripted Admin session map the cluster network. "Bounded by human cadence" is not a control. | Binding check is the **first** step of test-connection (mismatch ⇒ zero bytes sent); a test target must match a credential's `allowed_hosts` **or** the operator allow-list, otherwise return one collapsed status `blocked_by_policy` with no timing; per-session + global rate limit; only echo fields validated against the contract patterns (`supported_versions` `^[0-9]+$`); drop `latency_ms`/`tools_present` unless the response conformed. | `spec-product-knowledge.md` US-PK10, ADR-0013 consequences |
| **A3** | **Egress guard (ADR-0013 decision 3) is incomplete.** Always-deny omits cloud metadata endpoints that sit inside the "allowed by default" private ranges (AWS IMDS IPv6 `fd00:ec2::254`, Alibaba `100.100.100.200`, Azure wireserver `168.63.129.16`, OCI `192.0.0.192`), plus NAT64 `64:ff9b::/96`, 6to4, IPv4-compatible forms, non-canonical literals (`2130706433`, `0x7f.1`, IPv6 zone ids). `httpx.AsyncClient` defaults `trust_env=True`, so `HTTP(S)_PROXY` silently bypasses resolve-and-pin. No cross-job rate bound exists on the customer's knowledge system. | Add the metadata/transition ranges to the fixed deny set (settled "private allowed" does not require allowing them); `trust_env=False` on the client; a process-level per-endpoint `asyncio.Semaphore` and honoring `rate_limited` with a short in-process cool-down now (not "on evidence"); canonicalise hosts (lowercase, IDNA/punycode, trailing-dot, port, IP literal) in **one** function used at write time, job time and the transport. | ADR-0013 decision 3 (tightening, no reversal) |
| **A4** | **Config-change audit is not reconstructable.** `config_routes.set_config` audits `{scope, key}` only; this key lets an Admin repoint an endpoint or `credential_ref`. | For `product_knowledge_provider`, audit metadata carries old/new `endpoint_url` **host**, `credential_ref`, `enabled` (never a token). | `spec-product-knowledge.md` US-PK8/PK9 |
| **A5** | **Redaction can be disabled and is fatal-by-design.** With `secret_redaction_enabled=False` the `NoOpTextRedactor` lets the PR title leave the deployment unredacted; `findings/redact.py` raises on subprocess failure, contradicting "the provider branch never raises". | When redaction is disabled the provider branch is skipped with a distinct outcome (`redaction_disabled`); a redactor exception inside the branch maps to a typed outcome (`provider_error`), never propagates. State in the spec that title/query redaction is a precondition of the feature, not best effort. | `spec-product-knowledge.md` US-PK2, outcome enum |

Also recorded as **accepted limitations to state explicitly in the spec and
runbook** (security review, minor): `credential_ref` is global — an Admin can
point org A's repo at org B's credential (single trust domain per ADR-0004);
`GET …/credentials` lists credential names to Viewers and bound hosts to
Admins only (decided in Phase 6); the whole credential map is in both Worker and
Admin API pod environments (Admin API now holds provider tokens).

## Contract fixes and gaps this plan carries (called out, not buried)

| # | Fix / gap the spec closed | Where it lands |
| --- | --- | --- |
| F1 | **`title` added (required) to every search/fetch item**, plus `updated_at` on search items; `url`/`score` optional-nullable. ADR-0003's `{id, snippet, source_type, url, score}` cannot support a deterministic citation. Not a v1→v2 break: the contract is versioned `1` *before any provider exists*, and no consumer implements the old shape (F3). knowhow's wrapper must compute a title (Workstream K input). | Phase 1 (models + schemas), Phase 3 (citation), Phase 5 (conformance asserts `title`) |
| F2 | **Fourth tool `product_knowledge_contract`** declares `supported_versions` (not `serverInfo`, which MCP says must not drive behavior). Cached ~10 min, invalidated by `unsupported_version`. | Phase 1 (model), Phase 4 (negotiation + cache), Phase 5 |
| F3 | **knowhow does not implement the contract today** — it exposes `knowhow_search`/`knowhow_fetch`/`knowhow_related` (different args, cursor pagination, TOON text). "Works out of the box" is false until knowhow registers the contract tools. | Workstream K; acceptance signal is Phase 5's nightly live run |
| F4 | **Credential location contradiction** (ADR-0003/c4 said Postgres; `models.py`/design goal 7/Helm say bootstrap-only). Resolved as reference-in-Postgres, token-in-Secret with mandatory `allowed_hosts` binding. | Phase 6 |
| F5 | **PR title, body and head ref captured nowhere** (`PullRequest` models only `number`, `head.sha`, `base.sha`). | Phase 2 |
| F6 | **`OrgRepoTeamSetting` has no runtime reader** (`PostgresSettingRepository` exposes only `get_config_tree`/`upsert`). | Phase 6 |
| F7 | **Privacy claim reversed**: "No new data leaves the deployment on the retrieval side" (`spec-context-builder.md`) is no longer true with a provider configured. | Phase 8 (UI), Phase 7 (minimum runbook), Phase 9 |
| F8 | **Mechanical doc edits outstanding**: `operations.md`, `spec-observability.md` catalog, ADR-0005/0011 scope lists, `.env.example`/Helm values, `platform/docs/vision.md` line 57. | Phases 1, 6, 7, 9 |
| F9 | **Two-way failure isolation is not free**: `_orchestrate` runs under one `asyncio.wait_for` and `run()` replaces the whole bundle on any typed failure. | Phase 7 |
| F10 | **`ContextBundle.item()` is positional** (`id` == index) and returns `ContextBundleItem \| None`. Provider ids continue after code ids. | Phase 3 |
| F11 | **Config-tree rollback**: `get_config_tree` returns raw rows without registry validation, so after a code revert orphaned `product_knowledge_provider` rows remain un-editable/un-deletable via the API (only writes validate). | Phase 6 test; Phase 9 runbook SQL cleanup |
| F12 | **Helm optional-env trap**: `review.credentialEnvVar` always emits a `secretKeyRef` with no `optional`, so a new always-rendered credential breaks every existing install on upgrade. | Phase 6 |

### Workstream K — knowhow contract tools (cross-repo, not a phase of this plan)

knowhow is a separate repo/product (`builder/knowhow`, TypeScript, TS MCP SDK
`^1.25`, its own `CLAUDE.md`/docs rules). It must register
`product_knowledge_contract`, `_search`, `_fetch` (and `_related`) as thin
wrappers returning `structuredContent`, computing `title`. This plan does
**not** implement it; it publishes the schemas (Phase 1) and the runnable
suite (Phase 5) as its target, and uses the Phase 5 nightly live run as the
acceptance signal. Merging this repo's phases does not depend on it; the
claim "knowhow works out of the box" does. File the ticket in knowhow's
tracker before Phase 5 lands.

## Scope

**In scope** — the spec's Units A–I, sequenced 1:1 as Phases 1–9 (A→1,
B→2, C→3, D→4, E→5, F→6, G→7, H→8, I→9), the `product_knowledge_enabled`
kill switch, Amendments A1–A5 once confirmed, and the in-repo doc
reconciliation items above.

**Out of scope** — the spec's out-of-scope list applies unchanged
(Memory feedback; calling `related`; query-rewriting LLM / sending
symbols/diff; sending the PR description; OAuth/client-credentials; non-tool
MCP features; cross-job result caching; per-provider adapter classes;
multiple providers per repo; per-org RBAC; provider content in
`intent_summary` / Phase 12 walkthrough). Workstream K and the
`platform/docs/vision.md` edit live in other repos.

**Priority inside the plan (per the spec's Must/Should split):** Must —
Phases 1–7 core. **Slippable (Should-have), must not gate a Must phase:**
`test-connection` route + its status set + audit row (Phase 6b), the
dashboard row (Phase 7b), the Admin UI panel (Phase 8), the nightly
live-conformance hook (Phase 5b). If Phase 8 slips, Phase 9 proceeds without
its UI assertions.

## Assumptions this plan makes (flagged, not decided)

1. **Phases are not strictly linear.** Dependencies: 1 and 2 independent; 3
   needs 1+2; 4 needs 1; 5 needs 1+4; 6 needs 1+4; 7 needs 3+4+6; 8 needs 6;
   9 needs the rest it documents. 4 can run in parallel with 3.
2. **No schema-generator script.** A single `generate_schemas()` function
   (pydantic `model_json_schema()` + `json.dumps(sort_keys=True, indent=2)`)
   lives in the drift test module; running it with an `--update`-style env
   flag rewrites the committed files. Smaller than a separate script.
3. **Write the two-era client test first, escalate if it cannot pass.** No
   separate "spike" deliverable: the first `test_client.py` case runs the
   `mcp` v2 `Client` (`mode="auto"`) against both fake-provider eras over a
   caller-supplied `httpx2.AsyncClient(trust_env=False)` (mcp 2.x's HTTP stack is `httpx2`) with a pinning
   transport, redirects off, the `Authorization` header attached **per
   request in the transport after the host check** (never a client default
   header), and asserts the SDK makes no request except through the injected
   client to the bound host (no OAuth/protected-resource discovery on 401,
   no SSE `endpoint` event to another host, `$ref` schema resolution
   local). If it cannot pass, that is a **blocker raised against ADR-0013**.
4. **Per-endpoint client via a consumer-defined factory.** The endpoint and
   credential vary per (org, repo) at job time, so `ProductKnowledgeProvider`
   (negotiate/search/fetch) cannot be a single injected instance. Phase 3
   defines, at the consumer in `review/product_knowledge.py`, a
   `ProviderOpener` Protocol `(endpoint, credential) -> ProductKnowledgeProvider`
   plus a narrow `EffectiveProviderResolver` Protocol (the step depends on
   these; `review/` imports nothing from `tenancy`). Composition roots wire
   the adapter-backed opener and the `tenancy`-backed resolver.
5. **Negotiation cache is an injected, bounded object**, not module state:
   a small clock-injectable TTL map (10 min, size-bounded) keyed by
   `(endpoint_url, credential_ref)` — never the token — with a single-flight
   lock so concurrent cold jobs do one `product_knowledge_contract` call.
   Owned by the composition root, passed into the opener. Test-connection
   bypasses it.
6. **Effective-setting resolver is a concrete class mirroring
   `llmgateway/routing.py`'s `FallbackModelRoutingResolver`** (bounded
   `asyncio.wait_for`, 2 s default like `DEFAULT_MODEL_ROUTING_TIMEOUT_SECONDS`,
   broad-except fail-open, closed-set `{source}` counter), reusing
   `tenancy.settings.resolve_setting` and `adapter/storage/tenancy.py`'s
   `_to_setting_row` mapper — no second row shape, no new precedence logic.
   It runs **inside the provider branch's deadline**, not serially in front
   of code retrieval. The Admin preview uses the strict path/tree data, never
   the fail-open one.
7. **Write validation hook = one optional async callable for one key**,
   `(scope, value) -> None` raising `SettingValidationError`, passed to
   `create_config_router` and applied only for `product_knowledge_provider`
   (it needs scope to reject team-scope writes). No mapping, no sync/async
   duality.
8. **The `.env.example` check is an explicit assertion, not an extension of
   `apps/test_env_example_sync.py`** (which excludes `AdminAPISettings`): one
   test asserts the new `REVIEW_WORKER_*` and `REVIEW_ADMIN_API_*` variables
   appear in `.env.example`, independent of unrelated drift.
9. **E2E provider runs as a host process, not a Compose "fake-provider
   container"** — a **deviation from the spec's wording** called out for the
   owner: it follows `tests/e2e/conftest.py`'s `stub_llm_server` precedent
   (host-bound, reached via `host.docker.internal` / `extra_hosts:
   host-gateway`), adds no Compose service, and depends on
   `product_knowledge_allow_private_networks=true` being the default.
10. **Nightly live conformance extends `.github/workflows/e2e.yml`** with a
    step guarded by `if: github.event_name == 'schedule' ||
    github.event_name == 'workflow_dispatch'`, `PK_LIVE_URL` set, and
    `continue-on-error: true` (that file also triggers on `push: main`, so
    "conditional on the variable" alone would gate merges). `apps/test_github_workflows.py`
    gets a minimal assertion of those guards and of absence from `ci.yml`.
11. **`ContextBundle` gains one optional sub-model** (`product_knowledge:
    ProductKnowledgeSection | None` holding `label` + `items`), not two
    parallel fields. Provider items are **never** appended to `items`, so the
    existing code-bundle budget math, `render_context_bundle_section` and
    existing histograms are untouched. A **separate**
    `product_knowledge_item(id)` accessor avoids changing `item()`'s return
    type; one `next_id` helper feeds both `assemble_context_bundle` and
    provider assembly.
12. **The fake provider lives under `adapter/product_knowledge/testing/`**
    (not the production client's import path; `FAULT:` handling never
    importable by the production client) and is excluded from the Docker
    image and `[tool.mutmut] source_paths`; verify against the `Dockerfile`
    `COPY` lines and `pyproject.toml` during Phase 4. Prefer building it on
    the `mcp` SDK's server classes over hand-rolling two protocol eras with
    `http.server`.
13. **The PR title is persisted un-redacted in `job_queue.payload`** and
    forwarded through every `JobIdentity` step output; only the outbound
    query is redacted, at formulation time. Low severity (already
    repo-visible, customer's own Postgres) but flagged. The PR **body** is
    parsed to keys at the webhook boundary and never persisted.
14. **Settings placement:** the credential map (one shared pydantic
    submodel + validator, defined once and imported by both settings
    classes), `product_knowledge_enabled`, `allow_private_networks` and the
    allow-list go on **both** `WorkerSettings` and `AdminAPISettings`; the
    branch `deadline` and the `call < deadline < internal_timeout` validator
    go on the **Worker only**. A malformed map or allow-list on the **Admin
    API fails open** (log; the routes stay mounted, the credentials list
    reports `[]` and test-connection reports `disabled`) rather than aborting
    boot — an optional feature must not take down the config/audit UI; on the
    Worker it fails fast naming the problem, never the value.
15. **Outbound telemetry for job-level outcomes uses `operation="job"`**;
    tool calls use `contract|search|fetch` (`related` is out of scope and never
    called; no `discover`/`connect` operation is recorded), so the
    "call rate" panel is not polluted by non-calls. Test-connection records
    the same instruments with `origin="test_connection"` (a closed second
    label) so Worker rate panels exclude it.

## Phase 1 — Contract models, JSON Schemas, drift guard, contract docs (Unit A, US-PK16)

**Why first:** the contract is the artifact every other phase and the
cross-repo knowhow work builds against; no dependencies; cheapest place to
catch a wire-shape mistake (F1/F2).

- New `review/product_knowledge.py` — **wire models only in this phase**
  (pure pydantic, zero infra imports): `ContractDeclaration`
  (`supported_versions`, non-empty, `^[0-9]+$`), `SearchRequest`/
  `SearchResponse`/`SearchItem` (**required `title`**; optional-nullable
  `url`, `score`, `updated_at`; `source_type` `^[a-z][a-z0-9_.-]{0,31}$`),
  `FetchRequest`/`FetchResponse`, `RelatedRequest`, `ProviderError` (closed
  `code` set), a `NegotiationResult` model (chosen major, provider's
  validated `supported_versions`, tools present — so test-connection needs no
  adapter-specific entry point), and the limits constants (256 KiB, snippet
  1000, fetch 4000, title 200, url 2048, `TOP_K = 5`). Fixes F1/F2.
- Committed JSON Schemas under
  `docs/reference/product-knowledge-contract/v1/` (one per tool input and
  output) plus `examples/` (valid + invalid) and a `README.md` stating the
  versioning policy (ADR-0013 decision 4, copied not redefined) and the
  `title`-required note.
- Register the module in [ADR-0005](../adr/0005-pure-no-io-logic-unit-tested.md)'s
  "Scope, extended per phase" list (also needed because mutation testing,
  `scripts/run-mutation-tests.sh`, covers pure modules only via unit tests).

**Tests:** Unit (`adapter/product_knowledge/test_contract_schemas.py`, new —
committed == generated per Assumption 2; every valid example validates and
every invalid fails; `title` required; unknown extra fields ignored by the
tolerant models). **Integration link** (testing.md: every story maps to an
integration test): Phase 5's conformance suite and Phase 4's client test
validate live fake-provider responses against these committed schemas, and
that link is the US-PK16 integration evidence.

**Observability & rollback:** documentation + an unused module; reversible
by reverting the commit.

**Acceptance:** US-PK16 passes (committed == generated; examples validate;
README states policy) and the Phase 4/5 schema-validation link exists.

## Phase 2 — PR metadata capture (Unit B, US-PK2 capture half; gated by A1)

**Why second (independent of Phase 1):** query formulation (Phase 3) and the
A1 exposure controls need title, head ref, keys and fork/private flags, and
*nothing carries them today* (F5).

- `adapter/github/schemas.py`: `PullRequest` gains `title`, `body` (optional);
  `PullRequestHead` gains `ref` and the head repo's fork/full-name needed for
  the A1 fork check — all optional, same "accepted-but-degraded, never a 4xx"
  discipline `sha` has (`extra="ignore"`). **`pr_is_fork` derivation (pinned,
  fail closed):** `head.repo.full_name != base.repo.full_name` **or**
  `head.repo.fork is True`; when the head repo is absent/null (deleted fork)
  it is `True` (skip), never `False`.
- New pure `ingestion/pr_metadata.py`: whitespace-normalized title (≤300,
  `" ".join(title.split())`), `extract_issue_keys(title, head_ref, body) ->
  tuple[str, ...]` (de-duplicated, order-stable, ≤10; `PROJ-9` and `#41`
  forms; **no prefix filtering here — the effective
  `issue_key_prefixes` setting is only known at job time, so the filter is a
  separate pure `filter_issue_keys(keys, prefixes)` in the same module,
  applied by the Phase 7 branch**). **The body is read only inside this function and never
  returned/persisted/forwarded.** Pure → ADR-0005 entry. Phase 3's
  `formulate_query` does **not** repeat normalization/capping.
- `tenancy/resolution.py` `WebhookEvent` gains `pr_title`, `pr_head_ref`,
  `pr_issue_keys`, `pr_is_fork` (optional/defaulted, like `base_sha`).
- `apps/webhook_api/webhook_routes.py`: `_build_webhook_event` populates them
  (keys extracted here, body dropped); `_review_enqueue_payload` adds
  `pr_title`, `pr_head_ref`, `pr_issue_keys`, `pr_is_fork` to the `REVIEW`
  payload only (`INDEX`/`MEMORY_REACTION_POLL` never read them);
  `repo_private` is already carried (`private`).
- `review/step.py` `JobIdentity` gains the optional fields (the documented
  `base_sha` precedent). Optional fields are **not** auto-carried, so the
  three hand-constructing sites forward them explicitly:
  `review/prefilter.py` (~285), `review/static_analysis.py` (~258),
  `review/context_builder.py` (~604). `intent_summary`/`review_generation`/
  `verification` use `job_identity_fields(...)` and pick them up
  automatically. Update the stale "seven fields"/"fourth site" docstrings in
  `step.py` and `context_builder.py`.
- **Rolling-deploy safety:** no model in the REVIEW chain sets
  `extra="forbid"` (only `frozen=True`), so a Webhook-first deploy is safe
  (an old Worker ignores new keys) and old payloads yield `no_query` later.

**Tests:** Unit (`ingestion/test_pr_metadata.py`, new — normalization, cap,
key de-dup/order/10-cap, `filter_issue_keys` (keys outside the prefixes
dropped; empty list ⇒ all kept), body never in output,
`Closes #41` + `bugfix/PROJ-9-cache`; **`pr_is_fork` matrix** — same-repo ⇒
`False`, cross-repo ⇒ `True`, `fork=true` ⇒ `True`, head repo missing ⇒
`True`). Unit (`review/test_step.py`, extended — a **parametrized test over
every `JobIdentity` subclass** — introspected — asserting the new fields
round-trip through its Output construction, so a future step cannot silently
drop them; plus a static check in the same file that no model in the REVIEW
chain has `extra="forbid"`).
Integration (`apps/webhook_api/test_webhook_routes.py`, extended — a signed
`pull_request opened` carrying title/head ref/fork flag/body-with-keys
produces a queued payload with the new fields and **no body field**, real
Postgres; a delivery missing them still enqueues). Integration
(`review/test_prefilter_step.py`, `test_static_analysis_step.py`,
`test_context_builder_step.py` — each forwards the fields).

**Observability & rollback:** additive optional fields; reversible by
revert. Queued jobs carrying extra keys are harmless to the old code
(static `extra` check above, not a claim).

**Acceptance:** webhook delivery ⇒ queued payload carries title/head ref/
issue keys/fork flag and no PR body; the all-subclasses forwarding test
passes.

## Phase 3 — Domain logic: query, exposure gate, fusion, prompt, citation (Unit C, US-PK1–3, PK6, PK7; gated by A1 — A5's `redaction_disabled` outcome is only *defined* here, produced and tested in Phase 7)

**Why third:** depends on Phases 1+2. All pure logic, fully provable before
any network adapter exists.

- Extend `review/product_knowledge.py`:
  - `ProductKnowledgeProvider` Protocol (`negotiate() -> NegotiationResult`,
    `search`, `fetch`), `ProviderOpener` and `EffectiveProviderResolver`
    Protocols (Assumption 4) — all defined at the consumer. `related` is
    deliberately absent. **A fake-backed test in this phase exercises them**
    (so the Protocols are not consumerless until Phase 7).
  - `ProviderOutcome` closed enum: `ok, empty, partial, no_query,
    not_configured, disabled, redaction_disabled, skipped_public_repo,
    skipped_fork, timeout, unreachable, auth_failed, blocked_egress,
    credential_unavailable, unsupported_version, deprecated_version,
    malformed, oversized, provider_error` (the config-class values
    `credential_unavailable`, `blocked_egress`, `unsupported_version`,
    `auth_failed` and `redaction_disabled` are warned, not just counted —
    Phase 7).
  - `should_query(repo_private, pr_is_fork, setting) -> ProviderOutcome | None`
    — pure A1 gate (private-or-`allow_public_repos`; fork skip).
  - `formulate_query(title, issue_keys) -> str | None` — pure; normalized
    title + keys (≤512 total). **Redaction is not here**: the existing async,
    subprocess-backed `findings.redact.TextRedactor` runs in the step; this
    function stays I/O-free. `None` ⇒ `no_query`.
  - `normalize_results(...)` — tolerant consumer: truncate; drop invalid
    items; `source_type` pattern failure ⇒ `other`; **the single URL
    sanitizer** (`urllib.parse.urlsplit` + `quote`; scheme/length/userinfo/
    control-char; drop URL keep item) used by both normalization and
    citation (no second implementation); control-char strip including `\n`,
    `\r`, U+2028/2029; all-invalid ⇒ `malformed`; some-invalid ⇒ `partial`.
  - `assemble_product_knowledge(...)` — 5 items, ≤1,000-char snippets,
    ≤4,000-char fetched content for the top 2, 12 KiB ceiling,
    lowest-ranked-first, independent of the diff-proportional code budget.
- `review/context_bundle.py`: `ContextBundle.product_knowledge:
  ProductKnowledgeSection | None = None` (Assumption 11); separate
  `product_knowledge_item(id)` accessor; shared `next_id` helper;
  `render_product_knowledge_section(bundle)` returns `""` when absent so a
  no-provider prompt is **byte-for-byte** Phase 2's; otherwise the section is
  headed with the display name, JSON-serialized items only, framed with
  `INERT_REFERENCE_DATA_FRAMING` plus "do not quote provider content
  verbatim" (stated as best-effort; the real controls are A1).
- `review/review_generation.py` (~319) and `review/verification.py` (~263):
  append the new section after `render_context_bundle_section(...)`;
  `intent_summary` does **not** consume it.
- `review/citation.py`: second block `**Product knowledge (<display>):**`,
  lines `[<title>](<url>) — <source_type> · <host>`, provider id order, ≤5
  lines; title/display name through `escape_markdown` (tests cover `(`, `)`,
  `*`, `_`); URL from the single sanitizer; no URL ⇒ plain-text title; flag
  cleartext/IP-literal hosts in the label; **no snippet/content echoed**;
  unknown id ⇒ existing `warning` log. `review/output.py`'s
  `_apply_citations` needs no signature change.
- ADR-0005 entries for the new pure functions.

**Tests:** Unit (`review/test_product_knowledge.py`, new — gate matrix
(private/public/fork/opt-in), query determinism, `no_query`, every row of the
limits table, outcome classification, the URL sanitizer's hostile inputs; the
fake-backed Protocol test; "redaction ordering" is **not** a unit assertion —
see Phase 7). Unit (`review/test_context_bundle.py`, extended — separate
budget, ceilings, ids continue via the shared helper, code-item behavior
untouched, empty section is `""`). Unit (`review/test_citation.py`, extended).
Integration (`review/test_review_generation_step.py` **and**
`review/test_verification_step.py`, extended, LiteLLM cassette — hostile
snippet appears only inside the framed JSON section; no provider ⇒ request
body byte-identical to Phase 2's; US-PK1/6, both LLM paths). Integration
(`review/test_findings_output_step.py`, extended, recorded GitHub cassette —
posted citation block; US-PK3/6).

**Observability & rollback:** pure logic + rendering that is a no-op for an
empty section — no behavior change until Phase 7. Reversible by revert.

**Acceptance:** US-PK3 and US-PK6 pass; the byte-identical-prompt assertion
passes in **both** `review_generation` and `verification`. (US-PK1's
context-builder half and US-PK7's end-to-end id/`item()` assertions are
accepted in **Phase 7**, not here — the integration tests that prove them
live there.)

## Phase 4 — MCP client adapter, egress guard, fake provider (Unit D, US-PK5 transport classes, PK12, PK18, PK19; gated by ADR-0013 acceptance and A3)

**Why fourth:** the highest-risk unit (new SDK, two protocol eras, address
pinning); depends only on Phase 1, so it can run parallel to Phase 3 and
absorb SDK surprises early.

- `pyproject.toml`: add `"mcp>=2.3,<3"` (comment citing ADR-0013); refresh
  `uv.lock`. No separate import-boundary test (ADR + review enforce
  confinement to `adapter/product_knowledge/`).
- New `adapter/product_knowledge/egress.py`: **pure, unit-tested**
  `canonicalize_host`, `classify_address`, `host_matches_allowlist`
  (stdlib `ipaddress` — `is_loopback`, `is_link_local`, `is_unspecified`,
  `is_multicast`, `.ipv4_mapped` unwrap — and `fnmatch`/`str.endswith`; no
  hand-written CIDR table except the A3 additions: metadata IPs, NAT64,
  6to4, IPv4-compatible; non-canonical literals normalized via `ipaddress`
  after `getaddrinfo`, never URL string matching). One canonicaliser shared
  with Phase 6's write-time check and the job-time check. Connection-time
  guard in the `httpx2` transport after DNS resolution, resolved address
  pinned; schemes other than `http`/`https` rejected; redirects never
  followed; private ranges allowed by switch; operator allow-list
  (`*.suffix` must not match the bare suffix or `evilexample.com`); bearer
  token never sent over cleartext `http` to a public address. **`trust_env=False`**;
  TLS verification always on.
- New `adapter/product_knowledge/client.py`: one generic
  `McpProductKnowledgeClient` implementing the Phase 3 Protocols via the
  injected opener — negotiation through `product_knowledge_contract`
  (highest common major; missing/malformed ⇒ `unsupported_version`;
  Assumption 5 cache with single-flight, key excludes token); `contract_version`
  on every call; only arguments the tool's own `inputSchema` declares; connect
  2 s / per-call 5 s enforced with `asyncio.wait_for` (httpx read timeouts
  alone are evaded by a slow-drip response); **256 KiB cap implemented as a
  byte-counting wrapper on the response stream in the injected transport**, so
  it covers SSE, `tools/list` and the fallback `initialize` — breach ⇒
  `oversized`; `RecursionError` on deeply nested JSON ⇒ `malformed`;
  **per-endpoint `asyncio.Semaphore`** (cross-job fan-out bound) and
  `rate_limited` ⇒ short in-process cool-down (A3); tool
  descriptions/annotations/`serverInfo` and provider error bodies are
  **never read for behavior or echoed**; log a negotiation-result *change*
  at info (era at debug). Classifies every failure into `ProviderOutcome`.
  No retry, no circuit breaker (spec; recorded triggers).
- New `adapter/product_knowledge/testing/fake_provider.py` (Assumption 12):
  real fake MCP server in **legacy `2025-11-25`** and **modern
  `2026-07-28`** eras with `FAULT:<mode>` injection (`slow`, `hang`,
  `status401`, `status500`, `redirect`, `huge`, `malformed`, `badversion`,
  `error_result`, `partial`, `input_required`, plus `chunked_no_length`,
  `gzip_bomb`, `deep_json`, `hostile_metadata`, `error_body_with_token`).
- Constructed from already-parsed config; **no env/file reads in the
  adapter**; wiring at composition roots in Phases 6–7.

**Tests:** Unit (`adapter/product_knowledge/test_egress_pure.py`, new —
`classify_address`/`host_matches_allowlist`/`canonicalize_host`,
parameterized: `::1`, `127.0.0.1`, `0.0.0.0`, `::`, `169.254.169.254`,
`fd00:ec2::254`, `100.100.100.200`, `168.63.129.16`, `192.0.0.192`,
`fe80::`, `::ffff:127.0.0.1`, NAT64, multicast, `100.64/10`,
`2130706433`/`0x7f.1`, userinfo, IPv6 zone ids, trailing dot, case, port;
allow-list negatives `evilexample.com`, `example.com.evil.tld`, bare
suffix; ADR-0005 entry — also puts this logic under mutation testing).
Integration (`adapter/product_knowledge/test_egress.py`, new, real sockets +
injected resolver — each class denied before any bytes; private allowed/
refused by switch; allow-list; rebinding connects to the validated address;
`3xx` not followed incl. a redirect carrying `Authorization` to another host;
token not sent cleartext-public; `HTTP_PROXY` env set ⇒ ignored
(`trust_env=False`); US-PK12). Integration
(`adapter/product_knowledge/test_client.py`, new, real fake-provider process,
**both eras** — the two-era first test of Assumption 3; full search+fetch
(US-PK19); highest common major / none ⇒ "no provider" / deprecated
(US-PK18); one case per US-PK5 failure class asserting the `ProviderOutcome`
and that nothing escapes; `hostile_metadata`/`error_body_with_token` ⇒ not in
outcome, logs or exceptions; `chunked_no_length`, `gzip_bomb`, `deep_json`,
`InputRequiredResult` at the fetch stage; concurrent cold start does one
negotiation call; semaphore bounds in-flight calls; responses validated
against the committed schemas — the Phase 1 integration link).

**Observability & rollback:** the adapter is constructed by nothing yet —
inert until Phase 7; reversible by reverting the commit and the
`pyproject.toml`/`uv.lock` change. No content or token is ever logged.

**Acceptance:** US-PK5 (transport classes), US-PK12, US-PK18, US-PK19 pass
against the fake provider; **a dedicated `/security-review` of this unit
(egress guard, credential handling) is completed before merge** (spec
sign-off requirement, scheduled here rather than left implicit).

## Phase 5 — Conformance suite (Unit E, US-PK17), then live-mode hook (5b, Should-have)

**Why fifth:** the suite is the Phase 4 client's checks pointed at a URL;
needs Phase 4's fake; it is the acceptance signal for Workstream K (F3).

- New `adapter/product_knowledge/conformance.py` + `test_conformance.py`,
  parameterized by `--pk-url` / `PK_TOKEN` env: contract tool present/valid;
  version `1` declared; `search` schema, ordering, limits (**strict** —
  provider exceeding the snippet cap fails; consumer stays tolerant);
  `title` on every item (F1); `fetch` of a returned id; unknown id ⇒
  `not_found` error shape; unsupported `contract_version` ⇒
  `unsupported_version`; `top_k` honored; response size within caps;
  `related` shape if present; auth: missing/invalid token rejected when the
  server claims auth. Also `python -m adapter.product_knowledge.conformance`.
- **5b:** live mode opt-in (`--pk-url`/`PK_URL`), skipped by default, never a CI
  gate (`testing.md`), wired per Assumption 10.

**Tests:** Integration (`test_conformance.py` — passes on the in-repo fake;
fails on **seeded-faulty fakes, one per check**, proving each check can
fail). Unit (`apps/test_github_workflows.py`, minimal — the nightly step
carries the `schedule`/`workflow_dispatch` + `continue-on-error` guards and
is absent from `ci.yml`).

**Observability & rollback:** test tooling + one conditional CI step; no
runtime code; a failing nightly live run is informational for Workstream K.

**Acceptance:** US-PK17 — suite passes on the fake in CI, fails on every
seeded-faulty fake; (5b) the live hook runs nightly against a configured
endpoint without gating CI.

## Phase 6 — Configuration: setting key, reader, credentials, Admin API (Unit F, US-PK8–PK11, PK13 config half; gated by A2, A4; 6b slippable)

**Why sixth:** the Worker needs an effective provider (reader + credential
map) before Phase 7; depends on Phases 1 and 4. First runtime reader of
`OrgRepoTeamSetting` (F6).

- `tenancy/settings.py`: add `ProductKnowledgeProviderSetting` to
  `SETTING_KEY_REGISTRY` under `product_knowledge_provider`: `enabled`,
  `display_name` (1–40, `[A-Za-z0-9 ._()-]`), `endpoint_url` (absolute
  `http(s)`, ≤512, no userinfo/fragment/query — pydantic `AnyHttpUrl`, like
  `llm_endpoint`), `credential_ref` (`[a-z0-9_-]{1,64}` | null), plus the A1
  fields `allow_public_repos: bool = False`,
  `issue_key_prefixes: list[str] = []`. Update the registry's stale "nothing
  reads these at runtime" comment.
- Effective-setting resolver per Assumption 6: a new keyed read on
  `PostgresSettingRepository` (platform/org/repo rows for one key, **not**
  `get_config_tree`) → `_to_setting_row` → `resolve_setting`; narrowest scope's
  whole object wins; repo `enabled:false` opts out; team scope ignored;
  fail-open with a **named counter** (`review.product_knowledge.config_resolution_total`,
  `{source}` closed set incl. `fallback_on_error`) **and a warning log** on
  the fail-open path (the read runs under `asyncio.wait_for` with the
  branch's *remaining* deadline, never its own fixed budget, so a hung
  Postgres cannot push the branch past the internal window), registered under a searched root (see Phase 7 — either
  the resolver lives under `review/`/`adapter/`/`apps/`, or
  `scripts/check-dashboard-metrics.py`'s `SEARCH_ROOTS` is extended with a
  test). A Postgres blip must be distinguishable from "not configured".
- Bootstrap credential map (Assumption 14): `product_knowledge_credentials:
  SecretStr | None` as one shared pydantic submodel + validator (native
  complex-type JSON decoding, `SecretStr` per token); **empty/whitespace
  string ≡ unset** (`.env.example`/Compose `${VAR:-}`/Helm `value: ""` all
  produce it); every credential needs non-empty `allowed_hosts` (no `*`, no
  scheme-bearing host); duplicate refs rejected. On the Worker: malformed ⇒
  fail fast naming the problem, never the value (pydantic `input_value` must
  not echo a token — canary test); on the Admin API: fail open + disable the
  routes. Plus `product_knowledge_enabled` (default `true`),
  `product_knowledge_call_timeout_seconds` (5),
  `product_knowledge_allow_private_networks` (true),
  `product_knowledge_endpoint_allowlist` (empty) on both;
  `product_knowledge_deadline_seconds` (10) **Worker only**. Worker
  validator `call < deadline < context_builder_internal_timeout_seconds` is
  **conditional on the feature actually in use** (as implemented: the kill switch on
  **and** (a non-empty credential map **or** a non-empty endpoint
  allow-list); an unauthenticated provider is reachable only via the
  allow-list; the branch is also bounded at runtime by
  `min(deadline, remaining internal budget)`) — an operator who already tuned
  `context_builder_internal_timeout_seconds` ≤ 10 must still start clean on
  upgrade with no map; alternatively derive the effective deadline as
  `min(10, internal − margin)`.
  `product_knowledge_enabled=false` on the Admin API: config PUT still
  stores values (day-two config is not gated), test-connection returns a
  clear `disabled` status, and the UI shows it.
- Write path (Assumption 7 hook, **A2/A4**): `credential_ref` existence and
  `allowed_hosts` binding checked with the shared canonicaliser
  (`WIKI.Example.com.`, `host:8443`, `host@evil`, `host.evil.com`, unicode
  lookalikes tested); team scope rejected for this key; **audit metadata for
  this key carries old/new `endpoint_url` host, `credential_ref`, `enabled`**
  (A4; never a token). Job-time re-check fails closed
  (`credential_unavailable`) — enforced in Phase 7.
- **6 (Must):** `apps/admin_api/product_knowledge_routes.py`:
  `GET /api/v1/product-knowledge/credentials` (`require_session`, Viewer
  allowed; names always, **bound hosts only to Admin** — decided here, not
  deferred, since Viewers have no use for them and the UI shows "bound
  host" only in the Admin dropdown; never tokens) and the effective-config data the UI needs (see Phase 8:
  preferred as an `effective` field per node in `GET /api/v1/config`,
  computed by the Python resolver, so TypeScript never re-implements
  precedence).
- **6b (Should, slippable):** `POST /api/v1/product-knowledge/test-connection`
  (`require_admin_role`; audit via `PostgresAuditLogRepository`; factory
  injected from `apps/admin_api/main.py`, not imported by the route module).
  Request body is `{endpoint_url, credential_ref}` of the form's *current*
  values; **A2 order: write-time binding check first (mismatch ⇒ zero bytes
  sent) → target must match a credential's `allowed_hosts` or the allow-list
  else a single collapsed `blocked_by_policy` with no timing → egress →
  negotiate (bypassing the cache) → one `search` of `connection test`,
  `top_k=1`**; per-session limit 6/min and a global limit 30/min (module constants, 429 on
breach; the `rate_limited` provider cool-down is 30 s), reusing the
`InMemoryRateLimiter` shape from `apps/webhook_api/rate_limiter.py` if it can
be lifted without an `apps/`→`apps/` import (else a ~20-line local copy,
noted in the PR); the response `status` is the closed set `ok`,
`blocked_by_policy`, `disabled`, `unreachable`, `no_contract`, `malformed`,
`auth_failed`, `unsupported_version`, `deprecated_version`, `timeout`; response echoes only
  contract-validated fields (`supported_versions` `^[0-9]+$`);
  `latency_ms`/`tools_present` only for a conforming response; `message` is
  review-generated. Same adapter path as the Worker, **not** a second probe.
  Emits the shared instruments with `origin="test_connection"`.
- Deploy surface (F8, F12): `charts/review/values.yaml`
  `productKnowledge.credentials` using the chart's `value`/`existingSecret`
  shape; **render the env var only when `value` or `existingSecret.name` is
  set** (**decided:** conditional render, not `optional: true` — an
  absent `secretKeyRef` is simpler to reason about than an optional one); `secret.yaml` writes the key only when
  non-empty; add the field to `$needsOwnSecret`; inject into Worker **and**
  Admin API Deployments. Note that `existingSecret` rotation does not roll
  pods unless the chart has a checksum annotation (verify) and that a new
  ref rolled onto the Worker but not the Admin API yields
  `unknown_credential_ref` 422s during rollout. `docker-compose.yml` and
  `.env.example` gain the variables.

**Tests:** Unit (`tenancy/test_settings.py`, extended — registry model
patterns incl. A1 fields; precedence/opt-out). Unit, **not** marked
integration (`apps/worker/test_settings.py`, `apps/admin_api/test_main.py` —
pure validators): malformed-map matrix (non-JSON, non-object, empty token,
empty `allowed_hosts`, `*`/scheme-bearing host, bad ref pattern, duplicate
refs) asserting the message names the problem and not the token (caplog +
exception text); **"internal_timeout=8, no map, starts clean"**; empty-string
≡ unset on both Worker and Admin API. Integration
(`adapter/storage/test_tenancy.py`, extended, real Postgres — keyed
effective read; narrowest wins; repo opt-out; DB error ⇒ fail-open + counter +
warning; a **hung repository** ⇒ review completes, code context
unaffected; `get_config_tree` tolerates an orphan `product_knowledge_provider`
row — F11). Integration (`apps/admin_api/test_config_routes.py`, extended —
US-PK8 422s; token never in a response/audit row; A4 audit metadata).
Integration (`apps/admin_api/test_product_knowledge_routes.py`, new — US-PK9
unknown ref / unbound host 422; **6b:** test-connection with a mismatched
host sends **zero bytes**; unbound target ⇒ collapsed status; rate limit;
each status class vs the fake provider; message never echoes provider text;
US-PK10). Integration (`apps/admin_api/test_role_enforcement.py`, extended —
Viewer 403 on PUT and test-connection; GET config/credentials succeed;
US-PK11). Integration (`apps/worker/test_main.py` composition root, real
Postgres — kill switch builds no client; startup warning). Explicit
assertion test that the new `REVIEW_WORKER_*`/`REVIEW_ADMIN_API_*` vars
appear in `.env.example` (Assumption 8). `charts/review/test-render.sh`
**default-values render has no product-knowledge env var and no
secretKeyRef** (F12).

**Observability & rollback:** writes audit-logged; the fail-open resolver
has a counter and warning; nothing consumes the provider until Phase 7 so
the phase is inert to reviews. **Real rollback once rows exist is
`product_knowledge_enabled=false` (restart) or a platform/repo-scope
`enabled:false` row (no restart) — not a code revert**, because orphaned rows
become un-editable via the API (F11); the runbook (Phase 7/9) documents a SQL
cleanup. Confirm `resolve_setting` honors a platform-scope `enabled:false` as
the fleet-wide no-restart off (add a test) or state it is absent.

**Acceptance:** the explicit `.env.example` assertion test, the F11 orphan-row
tolerance test and the test-connection **zero-bytes-on-mismatch** and
both rate-limit (6/min session, 30/min global ⇒ 429 each) tests pass; US-PK8, PK9, PK11 and PK13 (config half) pass as integration
tests against real Postgres via `testcontainers-python`; `helm template`/
`test-render.sh` passes; **a dedicated `/security-review` of the Admin
routes and credential handling is completed before merge**. (US-PK10 /
test-connection is accepted separately under 6b and does not gate this
phase.)

## Phase 7 — Context Builder wiring, isolation, telemetry (Unit G, US-PK4, PK5, PK13, PK14; gated by A1, A5; 7b slippable)

**Why seventh:** the first phase that changes review behavior; needs 3, 4
and 6. Composition + isolation + telemetry over already-proven pieces.

- `review/context_builder.py` (F9): add a **provider branch** run
  concurrently with code retrieval, with its own deadline
  (`min(product_knowledge_deadline_seconds, remaining internal budget)` so it
  can never exceed the 25 s window), restructuring `run()` so the branches
  fail **independently** (a code failure keeps the provider section and
  vice-versa). **Outcome contract pinned:** the existing
  `outcome`/`degraded_error`/`degraded_empty` label is computed from the
  **code branch only**; provider items are never counted in the existing
  `item_count`/`bytes` histograms or `context_bundle_item_count`; a separate
  provider outcome is recorded on its own instruments; the existing
  `degraded_error`/`degraded_empty` tests stay **unmodified** as the
  regression guard. The outer `wait_for` `TimeoutError` cancels only the code
  branch. Provider dependencies injected via constructor (`ProviderOpener |
  None`, `EffectiveProviderResolver`, redactor); `None` ⇒ no provider code
  path (`not_configured`) ⇒ byte-for-byte Phase 2.
- Branch flow: resolve effective config (inside the deadline) →
  `not_configured`/`disabled` → **A1 gate** (`skipped_public_repo` /
  `skipped_fork`) → **`filter_issue_keys(pr_issue_keys,
  setting.issue_key_prefixes)`** (A1(c); pure, Phase 2) → credential lookup + job-time binding check
  (`credential_unavailable`) → **A5**: redaction disabled ⇒
  `redaction_disabled`; redact title via the **same redactor instance** the
  worker already builds (a redactor exception ⇒ `provider_error`, never
  propagates) → `formulate_query` → `no_query` → negotiate → search → fetch
  top 2 in parallel (failed fetch keeps snippet) → normalize → assemble.
  The branch itself never raises.
- `apps/worker/main.py` `_build_review_registry` (~705): provider wiring when
  `product_knowledge_enabled` and the map parses; one startup `warning` when
  disabled (mirroring `_log_memory_suppression_disabled_warning`), and one
  when a provider map is set but the `context_builder` step is disabled (the
  provider is used only when the Context Builder runs — also stated in the
  UI/runbook). `outcome=disabled` per job is emitted when a provider is
  configured but the flag is off (visible rollback signal), not silence.
- Telemetry (ADR-0011/0012; `review/_telemetry.py`'s
  `suppress_telemetry_errors`; the span is created with the tracer directly,
  with retroactive start/end times, rather than via the shared span helper,
  because the provider branch's wall-clock window is known only after the
  fact): `review.product_knowledge.call_total`/`call_duration_ms`/
  `item_count`/`bytes` registered with **literal** `create_counter`/
  `create_histogram` names in a file under a drift-guard-searched root
  (`review/` or `adapter/product_knowledge/`). **Metric label allow-list
  (closed sets):** `operation` per Assumption 15; `outcome` ∈ the
  `ProviderOutcome` enum; `contract_version` ∈ `{"1", "none"}` (`"none"` when
  negotiation did not complete; a provider-declared major outside the host's
  supported set maps to `"unsupported"`, never a raw provider value);
  `origin` ∈ `{worker, test_connection}`. **One span named
  `review.product_knowledge`**, a child of the Context Builder step span
  (spec-observability: child span per macro step); the US-PK14 log field set.
  **Config-class outcomes (`credential_unavailable`, `blocked_egress`,
  `unsupported_version`, `auth_failed`, `redaction_disabled`) log at
  `warning` with `provider_ref`/`endpoint_host` (log fields only); transient ones, and
  `disabled` (a deliberate kill switch, already announced by the startup
  warning), at `info`.** No query/snippet/title/URL path/token/provider error body is ever
  an attribute or field; org/repo ids are log fields only.
- **7b (Should, slippable):** dashboard row in
  `charts/review/dashboards/review.json` showing **non-ok outcome rate split
  by outcome** (not just overall rate) so config-class failures stand out
  from `empty`; extend (not create) `scripts/test_check_dashboard_metrics.py`.
  No alert rules ship (none exist in this repo by convention) — the runbook
  entry says so and gives the PromQL to watch.
- Minimum runbook content lands **here**, not only in Phase 9, because this
  phase ships behavior and the kill-switch lever: how to read `call_total`
  outcomes, the kill switch and repo-level opt-out, credential rotation
  (Secret update + roll Worker and Admin API), and the "data leaves the
  deployment" statement (F7).
- Docs: `spec-observability.md` — add the product-knowledge instruments and outcome
  values to the fixed, reviewed metric/span allow-list its Privacy/Compliance
  section requires (no new user story); ADR-0005/0011 scope lists
  extended with `review/product_knowledge.py` (F8).

**Tests:** Integration (`review/test_context_builder_step.py`, extended —
real Postgres + fake provider **process**; deadlines/timeouts injectable so
the "never responds" case uses ~0.5 s, not 10 s): US-PK1 end to end; US-PK4
(no provider / repo opt-out ⇒ fake receives **zero requests**, LLM request
body identical to Phase 2); **US-PK2 query redaction — the fake provider's
received `query` contains no pasted secret from the PR title and no
body/diff/path text**; A1 matrix (private ⇒ queried; public ⇒ skipped;
fork ⇒ skipped; `allow_public_repos` opt-in ⇒ queried; prefix allow-list
filters keys) with zero requests on skip; A5 (`NoOpTextRedactor` ⇒
`redaction_disabled`, no call; redactor subprocess failure ⇒ `provider_error`,
job completes); US-PK5 every failure class ⇒ empty section, right outcome,
job completes, code context unchanged; partial; **two-way isolation**
(forced `ContextQueryError` keeps the provider section; failing provider
keeps code context; provider deadline cannot exceed the internal window);
US-PK7 end to end (ids continue; `item()` for a code id unchanged); the
existing `outcome_total` and the single existing log line are emitted
exactly once with unchanged values when the provider branch fails.
Integration (telemetry, in-memory OTel reader — US-PK14 allow-lists; no
sensitive text). Unit (pure validators stay unit: timeout-ordering,
conditional-validator cases). Integration (`apps/worker/test_main.py` — kill
switch makes no outbound call even with settings present; both startup
warnings). 7b: `scripts/test_check_dashboard_metrics.py` extended.

**Observability & rollback:** `product_knowledge_enabled=false` (restart) or
a platform/repo `enabled:false` row (no restart) restores exactly Phase 2
behavior; telemetry failures are logged-and-continued (ADR-0012).

**Acceptance:** US-PK1 (context-builder half), US-PK2 (query), US-PK4,
US-PK5 (job level), US-PK7, US-PK13, US-PK14 pass as integration tests; the
existing `degraded_error`/`degraded_empty` tests pass unmodified; (7b) the
dashboard drift guard passes.

## Phase 8 — Admin UI panel (Unit H, Should-have, slippable; US-PK8/PK10 at UI level, PK15 UI half)

**Why eighth:** a thin surface over Phase 6's APIs; slipping it must not
block Phases 7 or 9 (API-only configuration is valid).

- **Mount point (corrected):** `apps/admin_api/frontend/src/screens/Config.tsx`
  has no scope selector — it renders one `ScopeCard` per scope with a generic
  `SettingForm` over a closed `SETTING_KEYS` select. Add a
  `ProductKnowledgePanel` inside `ScopeCard` for platform/org/repo (not
  team), and either extend `SETTING_KEYS`/`SettingKey` in `api/types.ts` so
  the generic form cannot save a raw mis-shaped `product_knowledge_provider`
  value, or explicitly exclude that key from the generic select.
- **Effective-config preview:** consumed from the `effective` field Phase 6
  adds to the config tree (single source of precedence; no client-side
  reimplementation). Add the matching `ScopeSettings` type change.
- **Test-connection (6b):** posts the form's *current* values; the button is
  disabled/labelled while the form is dirty so an admin is never told "ok"
  for unsaved values; the same 422 mapping applies.
- **States enumerated** (loading/empty/error/success per fetch/mutation):
  credentials GET — own loading/error state with retry that does not blank
  the form, "None" stays selectable; stored `credential_ref` no longer in the
  operator map ⇒ "Unknown credential `x` (removed by operator)", not a silent
  empty select; test-connection — pending state, button disabled while
  pending, "up to 10 s" hint, abort on unmount/scope change, a rate-limited
  (429) state distinct from the probe `status` classes, `disabled` status
  when `product_knowledge_enabled=false`; save — pending/disabled, 422 per
  field, success `Notice`. Reuse `useAsyncData`/`StateViews`: **decided** —
  `ErrorState` gains an optional `title?: string` prop (default keeps the
  current copy) so the credentials fetch error renders inline with its own
  `useAsyncData` call, independent of the config-tree fetch. Test-connection
  is a small explicit state machine `idle | pending | result(status) |
  rate_limited | disabled`, reset to `idle` whenever the form becomes dirty
  or the scope changes (abort via `AbortController`); dirty = any field
  differs from the saved value for that scope. A stored `credential_ref`
  missing from the fetched list renders as a disabled option "Unknown
  credential `x` (removed by operator)". Viewers get credential names only
  (Phase 6), so their dropdown shows no bound hosts.
- **Accessibility:** 422 `detail` codes map to field ids via `aria-invalid` +
  `aria-describedby`, focus moves to the first invalid field on submit;
  test result is a `role="status"` polite region mounted *before* the result
  arrives, failures `role="alert"`; the disclosure is a `<p>` tied to the
  endpoint field via `aria-describedby`, not a tooltip; status shown with an
  icon/text, not color alone; add `vitest-axe` (already used in
  `Config.test.tsx`) to the new tests.
- **Viewer:** controls are `disabled`/`aria-disabled` with the existing
  "Viewers can see configuration but not change it." hint — a deliberate
  divergence from the screen's hide-the-form pattern (per US-A5 UX rule);
  credentials and effective preview still render (route is `require_session`).
- **Content:** render the server `message` plus a status badge rather than a
  frontend status→copy table (no drift with Phase 6's classes). The A1
  opt-ins (`allow_public_repos`, `issue_key_prefixes`) and the **egress
  disclosure** (redacted PR title and linked issue keys; never code, diff or
  description) use **one shared literal constant** that the vitest assertion,
  the E2E assertion and the runbook all match, so a copy edit cannot silently
  break US-PK15. Reserve space for the test result (min-height); use the
  existing `.setting-form` stack so long hosts wrap. Display name rendered as
  text.
- New client functions `getProductKnowledgeCredentials`,
  `testProductKnowledgeConnection`; new types `ProductKnowledgeCredential`
  (`{ref, allowed_hosts}`), `TestConnectionResult`; `Config.test.tsx`'s mock
  factory extended.

**Tests:** Frontend unit (`Config.test.tsx`, extended — `ErrorState` with and
without a `title`; test-connection state-machine transitions (`idle→pending→
result|rate_limited|disabled`, back to `idle` on dirty form or scope change);
credentials dropdown
shows bound hosts and "None"; stale ref; dirty-form disables test; 429;
each `status` class renders its server message; display name `<script>`
rendered as text; Viewer disabled; disclosure literal present; axe).
E2E: `tests/e2e/test_admin_ui.py` (existing file) gains a panel case — if
Phase 8 ships; otherwise no claim is made.

**Observability & rollback:** static frontend change; reversible by revert;
backend behavior is unaffected whether or not the UI ships.

**Acceptance:** vitest passes in CI's Frontend job (standalone — does not
lean on Phase 9); the `test_admin_ui.py` panel case passes when shipped.

## Phase 9 — Docs and E2E (Unit I)

**Why last:** documents and end-to-end-verifies shipped behavior; must not
be blocked by Phase 8 slipping.

- `docs/reference/operations.md` (extends the minimum content from Phase 7):
  one `## <symptom>?` entry per `outcome` operators will see
  (`unsupported_version`, `blocked_egress`, `credential_unavailable`,
  `auth_failed`, `timeout`, `malformed`/`oversized`, `skipped_public_repo`/
  `skipped_fork`, `redaction_disabled`), test-connection as first-line
  diagnostic (if 6b shipped), credential rotation, the **egress/data-leaving
  statement**, the accepted limitations list (global `credential_ref`,
  Viewer-visible credential names, map in both pod environments), the
  no-alert caveat with PromQL, and the SQL cleanup for orphaned
  `product_knowledge_provider` rows (F11).
- Provider-implementer guide (the Phase 1 README expanded — no stub earlier):
  the four tools, field semantics, conformance command, versioning policy,
  and the residual information-disclosure risk + A1 scoping guidance.
- `platform/docs/vision.md` line 57 still names `knowhow_search/fetch/
  related` as review's consumed surface — **another project root / separate
  git repo**; recorded as a follow-up there, not edited from here (F8).
- New `tests/e2e/test_product_knowledge.py`: fixture order — start the fake
  provider on a known port, inject `REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS`
  with `allowed_hosts=["host.docker.internal"]`, then `compose up`
  (Assumption 9; relies on `allow_private_networks=true` default). Repo
  configured through the **real Admin API**, PR with an issue key on a
  private test repo ⇒ posted finding carries
  `**Product knowledge (<display>):**`; second run with the provider stopped
  ⇒ review still posted, no failure surfaced. The "configure via the UI" and
  UI-disclosure assertions are **conditional on Phase 8**.

**Tests:** E2E per above (happy path + the failure mode that matters). A
test (`apps/test_product_knowledge_runbook.py`) that the runbook's literal
egress-disclosure statement exists — the shared constant from Phase 8 if it
shipped, else the same literal duplicated in the test — so US-PK15's runbook
half has an assertion, not a link-check.

**Observability & rollback:** documentation + one E2E test; reversible by
revert.

**Acceptance:** the E2E passes in the nightly/merge run; every runbook
`outcome` entry exists.

**Delivered (2026-10-11):** runbook entries and the shared disclosure literal
in `docs/reference/operations.md`; the provider-implementer guide in
`docs/reference/product-knowledge-contract/v1/README.md`;
`tests/e2e/test_product_knowledge.py` (provider as a host process, repo-scope
configuration through the real Admin API, provider-down run);
`apps/test_product_knowledge_runbook.py`. **Follow-up recorded, not done here
(other repo):** `platform/docs/vision.md` line 57 must reference review's
`product_knowledge_*` contract instead of `knowhow_search/fetch/related`.

## Security review

One pass across every new input/trust boundary (the spec's NFR table is the
source; restated as a checkable artifact, **corrected** from the first draft).

- **The "no tools/no write path ⇒ two trifecta legs absent" claim in the
  spec is wrong and is withdrawn here (A1).** The PR comment is the exfil
  channel; the PR title/diff/body/branch are attacker-controlled (any GitHub
  user, incl. forks on public repos); provider content is the private data.
  Controls: A1(a)–(c) (private-only unless opted in, skip forks, optional
  key-prefix allow-list), citation always names the source, the prompt asks
  for non-verbatim use (best effort — *not* a control), credential scoping
  guidance, repo-level opt-out. **Worst case without A1:** an unprivileged
  author names `HR-77` and the operator's token fetches it into a
  world-readable PR comment.
- **Customer-typed endpoint URL reachable by a platform Admin session is an
  SSRF / token-delivery primitive** (Phases 4, 6). Controls: A3's egress
  guard (always-deny incl. cloud metadata/NAT64/6to4/non-canonical literals,
  resolve-and-pin, no redirects, `trust_env=False`, allow-list, one
  canonicaliser) **and** mandatory `allowed_hosts` binding at write time
  (422), at test time (zero bytes on mismatch — A2) and at job time (fail
  closed). Defense in depth *under* an operator `NetworkPolicy`.
- **test-connection is an SSRF/port-scan oracle (A2):** target restricted to
  bound/allow-listed hosts, collapsed statuses otherwise, rate limited,
  only contract-validated fields echoed.
- **Tokens:** bootstrap `SecretStr` JSON map only; never in Postgres, API
  responses, audit rows, logs, spans, metrics; `Authorization` attached
  per-request in the transport after the host check; pydantic validation
  errors must not echo `input_value` (canary test). Adding a *new*
  credential needs an operator action. The whole map is in both Worker and
  Admin API pod environments (accepted widening, stated).
- **Prompt injection (Phase 3):** provider text is untrusted data —
  JSON-serialized only, inside the framed section, control chars stripped;
  the LLM call has no tools. **That limits what an injected prompt can *do*,
  not what it can *say* — see A1.** Tool descriptions, annotations,
  `serverInfo` and provider error bodies never reach the prompt, logs or
  behavior (tested in Phase 4).
- **Markdown/link/beacon injection in citations:** `escape_markdown` on
  title/label, URL from the single sanitizer, host displayed, cleartext/IP
  literal flagged, no image syntax, no content echoed, `\n`/`\r`/U+2028
  stripped so a title cannot break list structure. A `[title](url)` still
  links to a provider-supplied URL (phishing from a poisoned page) —
  mitigated by showing the host; residual accepted.
- **Response bombs / resource exhaustion (Phase 4):** 256 KiB cap as a
  stream-counting wrapper in the injected transport (covers SSE and
  `initialize`), `asyncio.wait_for` wall-clock timeouts (slow-drip safe),
  `RecursionError` ⇒ `malformed`, deep-JSON/gzip-bomb/chunked tests; per-job
  ≤3 in-flight **and** a process-level per-endpoint semaphore + `rate_limited`
  cool-down (cross-job fan-out can no longer DoS the customer's knowledge
  system for everyone).
- **Egress of PR-derived data (F7):** redacted title (≤300) and ≤10 issue
  keys only; disclosed in the UI and runbook. **Redaction is a precondition
  (A5):** disabled ⇒ no query; redactor failure ⇒ typed outcome. The PR title
  is persisted un-redacted in `job_queue.payload` (Assumption 13) — flagged.
- **Authz:** config PUT and test-connection Admin-only, Viewer 403 enforced
  server-side, CSRF via `require_session` for non-safe methods; config-change
  audit now records old/new host/ref/enabled (A4). `credential_ref` is global
  across orgs (single trust domain, ADR-0004) — accepted, stated in the
  runbook.
- **Security-review additions (2026-10-12, implemented with tests):**
  - `allowed_hosts` / allow-list entries are port-aware (`host:port` binds
    that port; a bare host matches any port, kept for compatibility).
  - A null `credential_ref` is accepted only if the endpoint host matches the
    operator allow-list or some credential's `allowed_hosts`
    (`unauthenticated_host_not_allowed`, 422).
  - When `issue_key_prefixes` is set, the query is key-only: free-text title
    queries are not sent.
  - test-connection has a per-user limiter in addition to the global one.
  - The analyzer subprocess environment drops the credential map.
- **Dependency risk:** `mcp` v2 is ~2 months old; `>=2.3,<3`, confined to
  one package behind the Protocol, two-era tests load-bearing.
- **Reviews scheduled, not implied:** `/security-review` on Unit D (Phase 4)
  and Unit F (Phase 6) are in those phases' acceptance criteria.

## Compatibility & rollout

- **No Postgres migration.** Reuses `org_repo_team_setting`; new job-payload
  fields are optional; `ContextBundle`/`JobIdentity` gain optional fields
  only. **Deploy order Webhook-first is safe** (no `extra="forbid"` in the
  chain — static check in Phase 2).
- **Zero-config upgrade (US-PK13):** no provider setting and no credential
  map ⇒ everything behaves as before, **including** an operator with a tuned
  `context_builder_internal_timeout_seconds` ≤ 10 (conditional validator),
  and an install whose Helm values/`.env`/Compose leave the new variable
  empty or unset (empty ≡ unset; no unconditional `secretKeyRef`).
- **Contract:** `title` required is not a v1→v2 break (nothing implements
  the contract yet; versioned `1` before any provider).
- **Kill switch:** `product_knowledge_enabled` (default `true`, inert
  without a provider; bootstrap, restart) on Worker and Admin API;
  platform/repo-scope `enabled:false` rows are the no-restart off.
- **Staged deploy:** Phases 1–6 add models/adapter/config/APIs nothing in
  the review path consumes; Phase 7 is the single behavior-changing link;
  8–9 follow. Phase 4 additionally gates on ADR-0013 acceptance; the
  amendment gates are listed above.
- **Dependency-failure containment:** a slow/blackholed/hostile provider
  costs ≤5 s per call / ≤10 s branch, concurrent with code retrieval and
  capped to the remaining internal window, degrading to "no provider
  context" — never a failed review. No retry, no circuit breaker in v1
  (recorded triggers); a per-endpoint semaphore + `rate_limited` cool-down
  bound fan-out (A3). The settings reader's Postgres dependency fails open
  inside the branch deadline.
- **Riskiest assumption (from the spec):** PR title + linked issue keys
  retrieve useful context often enough to be worth a call. Validate behind
  the kill switch on one pilot repo for two weeks via
  `call_total{outcome=ok|empty}` and the cited-item rate; query formulation
  is one pure function so it can change without touching the contract.
- **Other products touched:** knowhow (Workstream K, its own repo);
  `platform/docs/vision.md` follow-up.

## Traceability: spec user story → test

| Story | Phase | Test path | Level |
| --- | --- | --- | --- |
| US-PK16 | 1 (+4,5 link) | `adapter/product_knowledge/test_contract_schemas.py`; schema validation inside `test_client.py` / `test_conformance.py` | Unit (static) + Integration (fake-provider responses validated against committed schemas) |
| US-PK2 (capture) | 2 | `ingestion/test_pr_metadata.py`; `apps/webhook_api/test_webhook_routes.py` | Unit (pure) + Integration (webhook → queued payload, real Postgres) |
| US-PK2 (query, redaction, A1/A5) | 3, 7 | `review/test_product_knowledge.py` (pure); `review/test_context_builder_step.py` (fake provider sees redacted query; skip matrix) | Unit + Integration |
| US-PK1 | 3, 7 | `review/test_review_generation_step.py`; `review/test_verification_step.py`; `review/test_context_builder_step.py` | Integration (cassette; real Postgres + fake provider process) |
| US-PK3 | 3 | `review/test_citation.py`; `review/test_findings_output_step.py` | Unit (rendering) + Integration (recorded GitHub cassette) |
| US-PK6 | 3, 4 | `review/test_review_generation_step.py`; `review/test_verification_step.py`; `review/test_findings_output_step.py`; `adapter/product_knowledge/test_client.py` (hostile metadata/error bodies) | Integration |
| US-PK7 | 3, 7 | `review/test_context_bundle.py`; `review/test_context_builder_step.py` | Unit (pure) + Integration |
| US-PK5 (transport classes) | 4 | `adapter/product_knowledge/test_client_faults.py` | Integration (fake provider, fault injection) |
| US-PK12 | 4 | `adapter/product_knowledge/test_egress_pure.py`; `test_egress.py` | Unit (pure classifier) + Integration (real sockets) |
| US-PK18 | 4 | `adapter/product_knowledge/test_client.py` | Integration |
| US-PK19 | 4 | `adapter/product_knowledge/test_client.py` | Integration (two-era fake processes) |
| US-PK17 | 5 | `adapter/product_knowledge/test_conformance.py` | Integration (fake provider; live-mode opt-in) |
| US-PK8 | 6 | `apps/admin_api/test_config_routes.py`; `adapter/storage/test_tenancy.py` | Integration (real Postgres) |
| US-PK9 | 6 | `apps/admin_api/test_product_knowledge_routes.py`; `apps/admin_api/test_product_knowledge_routes_ordering.py`; `apps/admin_api/test_product_knowledge_routes_limits.py`; `apps/admin_api/test_config_routes.py`; `adapter/product_knowledge/test_client.py` | Integration (real Postgres; log-capture) |
| US-PK10 (6b) | 6 | `apps/admin_api/test_product_knowledge_routes.py` | Integration (fake provider process) |
| US-PK11 | 6 | `apps/admin_api/test_role_enforcement.py` | Integration |
| US-PK13 | 6, 7 | `apps/worker/test_settings.py` (incl. conditional timeout validator), `apps/admin_api/test_main.py` (pure validators, unit); `apps/test_product_knowledge_env_sync.py` (`.env.example` assertion, Assumption 8); `apps/worker/test_main.py` (composition root) | Unit + Integration |
| US-PK4 | 7 | `review/test_context_builder_step.py` | Integration (fake provider asserts zero requests) |
| US-PK5 (job level, isolation) | 7 | `review/test_context_builder_step.py` | Integration |
| US-PK20 (A1 exposure gate) | 2, 3, 7 | `ingestion/test_pr_metadata.py` (`pr_is_fork`, prefix filter); `review/test_product_knowledge.py` (gate matrix); `review/test_context_builder_step.py` (skip ⇒ fake provider receives zero requests) | Unit + Integration |
| US-PK14 | 7 (7b) | `review/test_context_builder_step.py` (in-memory OTel reader); `scripts/test_check_dashboard_metrics.py` | Integration + Unit (static drift check) |
| US-PK15 | 8, 9 | `Config.test.tsx`; `apps/test_product_knowledge_runbook.py` (asserts the shared disclosure literal appears in `docs/reference/operations.md`); `tests/e2e/test_admin_ui.py` (if Phase 8 ships) | Frontend unit + E2E |
| US-PK1–7 (happy path + provider-down) | 9 | `tests/e2e/test_product_knowledge.py` (new) | E2E |
