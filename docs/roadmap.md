# Roadmap

A high-level view of delivery order: what gets scaffolded first, what
capability lands in each phase, and what's deliberately deferred. This is a
sequencing document, not a project plan — no dates, no tickets. It answers
"what comes before what and why," not "who does what by when." For the
system being built, see [`reference/c4-architecture.md`](reference/c4-architecture.md);
for why it's built this way, see [`adr/`](adr/) and
[`research/research-current-art.md`](research/research-current-art.md).

Phases are dependency-ordered, not strictly sequential — later phases can
start once their prerequisites land, without waiting for every earlier phase
to be "complete."

---

## Phase 0 — Scaffolding

The non-feature foundation every later phase builds on. Nothing here is
customer-visible.

- Repo structure per [`rules/architecture.md`](rules/architecture.md): domain
  packages (`ingestion`, `indexing`, `review`, `llmgateway`, `findings`,
  `memory`, `tenancy`), adapter packages, the two entrypoints
  (`apps/webhook_api`, `apps/worker`).
- CI: `ruff` + strict type checker gate, unit test run on every push (per
  [`rules/testing.md`](rules/testing.md)'s CI-gating section).
- Docker Compose stack for local dev (Webhook API + Worker + Postgres/pgvector,
  LLM Gateway pointed at local LM Studio) — the same images Kubernetes runs in
  production ([ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md)).
- Postgres schema baseline (Alembic): org/repo/team tables, job queue
  (`SKIP LOCKED`-style), bootstrap config tables.
- LLM Gateway skeleton: LiteLLM wired in, one provider configured end-to-end
  ([ADR-0002](adr/0002-implement-in-python.md)).
- GitHub App skeleton: webhook signature verification, installation
  resolution, App manifest/callback.
- Helm chart skeleton: `ConfigMap`/`Secret` rendering, `existingSecret`
  support, `postgresql.enabled` toggle — doesn't need to be production-grade
  yet, needs to exist so later phases deploy the same way from day one.

**Exit criteria:** `docker compose up` runs a Webhook API + Worker + Postgres
that boots cleanly with no review capability yet.

---

## Phase 1 — Walking skeleton: the core review loop

Prove the full pipeline end-to-end with the simplest possible version of
each stage before adding sophistication to any one of them.

- Webhook API receives a PR event, enqueues a review job.
- Worker: pre-filter → **bare diff** (no index yet) → one LLM call → findings
  posted as inline PR comments + check-run status via the GitHub Adapter.
- Static analysis runner integrated as the free, deterministic first pass
  (design goal 4 — this ships *before* RAG sophistication, not after, because
  it's free and it's the trust anchor everything else gets checked against).

**Exit criteria:** a real PR against a real repo gets a real (if
unsophisticated) review posted by the platform.

---

## Phase 2 — Real indexing / RAG

Replace "bare diff" with the structural context every credible competitor
builds (research §2.1).

- Incremental indexing job: AST-aware chunking, per-node embeddings,
  call/import graph — kept fresh on every push. LLM-free by design — bi-encoder
  embeddings and static parsing only, no chunk-level summary generation,
  hierarchical summarization, or LLM-based entity extraction (see
  [ADR-0007](adr/0007-llm-free-indexing-deferred-rag-enrichment.md)).
- Context Builder queries the index during review (blast-radius lookups for
  changed signatures, not just the changed hunk).
- Review job upgraded to consume built context instead of the bare diff from
  Phase 1.

---

## Phase 3 — Verification / multi-pass pipeline

Add the structurally-separate check that the research (and design goal 6)
identifies as the difference between "thorough" and "diff-into-an-LLM."

- Multi-pass pipeline: intent summary → review generation → verification.
- Verification runs as a distinct call against a fixed rubric — never
  self-critique.
- Findings merger: dedupes, applies confidence/severity, drops low-value
  findings before a human sees them.

---

## Phase 4 — Memory (continual learning, human-validated)

- Memory schema: `pending`/`approved` status, provenance, ranking-time decay
  (half-life, steeper for `pending` — see the Memory bullets in
  [`reference/c4-architecture.md`](reference/c4-architecture.md)).
- GitHub Adapter's write path: captures reactions/replies/thread-resolution
  on the platform's own posted comments as candidate Memory entries.
- Findings merger & verify start *reading* approved Memory entries (design
  goal 6 — reads only what's been approved, never self-trusts).
- Admin API surface: list/approve/reject/delete, scoped by org/repo/team —
  API-only at this point, the UI catches up in Phase 5.

*Open: exact column-level schema for Memory entries and per-team rules, and
how a dismissal becomes the similarity-check signal design goal 6 commits
to — see `c4-architecture.md`'s "Open questions."*

---

## Phase 5 — Admin UI (MVP)

Confirmed in scope for the MVP release, not a fast-follow.

- Org/repo/team configuration screens.
- Memory approve/reject/delete screen, built directly on Phase 4's admin API.
- Model routing configuration (which endpoint/model serves which pipeline
  stage).

*Open: whether SSO/OIDC ships in this same MVP or immediately after — see
`c4-architecture.md`'s "Open questions."*

**— MVP line —** Phases 0–5 are what a pilot customer needs: a working
review loop, real context, verification, basic learning, and a UI to
configure and audit it. Everything below is real, accepted scope, but not
MVP-blocking.

---

## Phase 6 — Multi-org tenancy

Per [ADR-0004](adr/0004-multi-org-tenancy.md): org joins repo/team as a
scoping key, modeled as one customer's internal structure, not
multi-tenant-SaaS isolation between separate companies.

- Org-scoping layer across findings, Memory, and config.
- Multiple GitHub App installations under one deployment.
- RBAC: default visibility stays within an org; cross-org visibility is an
  explicit admin grant, not the default.

*Open: exact RBAC design for org-scoping — flagged in ADR-0004's
consequences and `c4-architecture.md`'s "Open questions."*

---

## Phase 7 — Optional external product-knowledge augmentation

Per [ADR-0003](adr/0003-knowhow-product-knowledge-augmentation.md) — entirely
optional, review is fully functional with none of this configured, and not
locked to any single provider.

- Define and version review's own product-knowledge MCP contract
  (`product_knowledge_contract` for version negotiation,
  `product_knowledge_search`/`product_knowledge_fetch`, and the optional
  `product_knowledge_related`).
- Context Builder's additional MCP client call against a configured
  provider's endpoint.
- Context fusion: the configured provider's results kept as a distinct,
  labeled source (labeled by provider display name) alongside review's own
  code-derived context.
- Per-org/repo provider endpoint + credential + display-label configuration.
- knowhow ships as the contract's reference implementation — the first, and
  for now only, provider — but is configured through the same generic path
  any future provider would use, with no knowhow-specific code in the
  Context Builder.

Specified in [`specs/spec-product-knowledge.md`](specs/spec-product-knowledge.md)
(with [ADR-0013](adr/0013-product-knowledge-mcp-client-sdk-credentials-and-egress.md)).

**Status (2026-10-11):** implemented in this repo across the plan's Phases 1-9
(`plans/plan-deliver-product-knowledge.md`):
contract + schemas, PR metadata capture, domain logic, MCP client + egress
guard, conformance suite, configuration/Admin API, Context Builder wiring and
telemetry, Admin UI panel, runbook and E2E. Remaining before it is
"done": ADR-0013 sign-off (still `PROPOSED`), knowhow registering the contract
tools (cross-repo Workstream K; the nightly live-conformance run is its
acceptance signal), the two-week pilot behind the kill switch, and the
`platform/docs/vision.md` follow-up (that file, in another project root, still
names `knowhow_search/fetch/related` as review's consumed surface and should
reference review's `product_knowledge_*` contract).

---

## Phase 8 — Production hardening

Ongoing, but concentrated once a real pilot customer is running the platform
in their own cluster rather than a demo Docker Compose stack.

- Helm chart maturity: health probes, resource defaults, upgrade paths,
  observability hooks — a first-class product surface per
  [ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md), not an
  afterthought.
- Per-pipeline-stage model routing tuned for cost/quality (cheap model for
  triage, strongest configured model for review generation + verification).
- Strong local reproducibility (Docker Compose mirrors production images) to
  compensate for the trade-off ADR-0001 accepted: no shared fleet visibility
  across customers.

---

## Phase 9 — Expanded static-analysis engine coverage

Commercial-parity gap (research §2.2): the Phase 1 static-analysis runner
shipped with two engines (Gitleaks + Semgrep, per
[`spec-review-pipeline.md`](specs/spec-review-pipeline.md)) to prove the
pipeline end-to-end, not as the intended ceiling — commercial tools bundle
15–50+ per-language engines. This is additive to the same step, not a
re-architecture, so it depends only on Phase 1's static-analysis runner and
the pipeline-step framework, and can start any time after Phase 1 without
waiting on Phases 2–8.

- New analyzer adapters behind the existing static-analysis step, one per
  language/category the customer base actually needs (e.g. JS/TS lint,
  Python lint beyond secrets/SAST, Go lint, IaC scanning) — each normalized
  into the same findings schema Gitleaks/Semgrep already produce, so the
  findings merger (Phase 3) needs no per-tool special-casing.
- Respect repo-native tool configs when present (`.eslintrc`, `.golangci.yml`,
  etc.) rather than a review-owned default ruleset silently overriding them —
  the pattern CodeRabbit documents (research §2.2).
- Per-analyzer enable/disable + timeout config, reusing the
  `*_enabled`/`*_step_timeout_seconds` convention `apps/worker/settings.py`
  already establishes for `static_analysis_enabled`.
- Analyzer selection scoped to languages actually detected in the repo —
  don't invoke irrelevant tools on every push.

*Open (for a future PRD): which languages/tools to prioritize first, ranked
by customer language mix, not built alphabetically; whether tool
versions/configs are deployment-pinned or customer-configurable; and how many
analyzer subprocesses the Worker can run concurrently before this needs its
own resource-isolation story.*

---

## Phase 10 — Whole-codebase duplication/clone detection

Commercial-parity gap (research §2.2, and the Codacy case study in
[`research-current-art.md`](research/research-current-art.md)'s Codacy
profile): Phase 2's embedding-similarity search (`spec-context-builder.md`
US4) only fires for chunks that actually changed in the current diff — it
cannot surface duplication that exists purely between two files neither of
which is in the PR. This is a distinct, deterministic finding-producing step,
not a Context Builder feature, so it belongs alongside Phase 9's
static-analysis expansion — but it depends on Phase 2's AST-chunked index
existing (it reuses that chunk structure rather than parsing separately), so
it can't start before Phase 2 lands.

- A new deterministic step (same "free layer" pattern as Phase 1) running a
  token/AST-level clone-detection algorithm across the whole indexed
  codebase, not scoped to the diff.
- Runs incrementally off Phase 2's already-incremental indexing job: compute
  clone candidates only for chunks that changed, compared against the full
  existing index, rather than a full-repo rescan on every push.
- Findings surface as a distinct `duplication` category with a location pair
  (the changed file plus the file(s) it duplicates), kept separate from
  correctness/security findings in the findings schema.
- Tunable minimum-clone-size threshold (likely per-language) to avoid
  flagging trivial boilerplate.
- Reuses Memory (Phase 4) unmodified: a dismissed duplication finding
  suppresses similarly-patterned future flags the same way any other
  dismissed finding does, with no special-casing needed.

*Open (for a future PRD): build vs. adopt an existing OSS clone-detection
engine (e.g. jscpd, PMD CPD) vs. a custom AST-hash approach reusing Phase 2's
chunker; whether a clone pair where neither file is in the current diff gets
surfaced as a PR-time finding at all, or only in a separate scheduled
"codebase health" report — this decides whether it's a pipeline step or a
standalone job; and the performance budget for the underlying full-repo
comparison.*

---

## Smell baseline and spec-compliance review (delivered, not a numbered phase)

Delivered per `plan-deliver-smell-and-spec-review.md`:
a fixed 12-smell baseline in `review_generation`'s prompt (`Finding.category =
"smell"`, severity capped, `REVIEW_WORKER_SMELL_BASELINE_ENABLED`) and a
`spec_review` step (`REVIEW_WORKER_SPEC_REVIEW_ENABLED`) that checks the diff
against the spec found for the PR. Both default on, with restart-only kill
switches. Complements Phase 11/12 rather than blocking them. Runbook:
[`reference/operations.md`](reference/operations.md).

---

## Phase 11 — Auto-fix / committable suggested-fix generation

Commercial-parity gap (research §3, tool profiles for CodeRabbit/Qodo/Bito/
DeepSource, all of which generate a one-click-committable fix): nothing in
Phases 0–8 produces a fix, only a finding. This depends on Phase 3's
verification pass existing — an unverified auto-generated fix is worse than
an unverified comment, so this is ordered strictly after verification is
solid, not before it.

- Findings merger (Phase 3) gains an optional "propose fix" sub-step: for
  findings above a confidence threshold, a distinct LLM call generates a
  patch scoped to the finding's file/lines — a separate call, never the same
  call that produced the finding (design goal 6).
- The generated fix is validated before ever being shown: syntax-checked, and
  re-run through the relevant static analyzer/verification rubric against the
  *patched* code, not just the original — the Copilot Autofix pattern
  (research §2.2).
- Delivered as a GitHub "suggested change" (one-click-committable inline
  suggestion) via the existing GitHub Adapter comment-posting path — no new
  delivery mechanism.
- Never auto-applied — always requires the PR author's explicit accept/commit
  action, per design goal 9's human-validates-before-anything-reshapes-state
  principle (already applied to Memory).
- Accepted vs. rejected fixes feed Memory (Phase 4) as `accepted_fix`
  entries — the schema already reserves this status; nothing populates it
  until this phase exists.

*Open (for a future PRD): which finding categories are eligible for auto-fix
first (security/style likely safer than architectural findings); how a
multi-file fix is represented, since GitHub's native suggestion UI is
single-hunk only; and the added per-stage model-routing/cost implication of a
second generation call per eligible finding, which ties into Phase 8's model
routing tuning.*

---

## Phase 12 — PR-level walkthrough/summary artifact

Commercial-parity gap (research §2.3, CodeRabbit's cohort-ordered walkthrough
and Sourcery's auto-generated diagrams): Phase 3 already computes an intent
summary internally to steer review generation, but never exposes it to the
PR author. This is "expose what Phase 3 already knows" as a customer-facing
deliverable — a delivery/formatting change, not new architecture — so it can
land any time after Phase 3, independent of Phases 4–11.

- Post Phase 3's intent-summary step output as a top-level PR
  comment/walkthrough, ordered logically (e.g. schema → logic → call sites →
  tests) rather than file-alphabetically.
- Optional architecture/sequence diagram generation (Mermaid) for PRs above a
  size/complexity threshold.
- Reuses the existing GitHub Adapter comment-posting path — one additional
  comment type, not a new integration.
- Config toggle per org/repo, following the existing `*_enabled` convention.

*Open (for a future PRD): whether this replaces or supplements the existing
findings-comment format; the diagram-generation LLM-cost tradeoff on large
PRs; and whether walkthrough content should also cite Phase 7's
product-knowledge context (linked specs/tickets) once that phase exists.*

---

## Deferred, not scheduled

Real, deliberate scope — not forgotten, not designed away — but not on this
roadmap because nothing above depends on them landing first.

- **GitLab/Bitbucket adapters.** The git-host boundary in the domain layer is
  already designed so these don't touch pipeline logic when they do land.
- **Vendor-hosted SaaS offering.** Explicitly deferred by
  [ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md) — may be added as
  an *additional*, not exclusive, offering once the self-hosted product is
  proven.
- **True multi-tenant isolation** (e.g. an MSP running one deployment for
  several separate client companies). [ADR-0004](adr/0004-multi-org-tenancy.md)
  explicitly does not provide this — would need revisiting, not a natural
  extension of Phase 6's model.
- **Optional, richer RAG enrichment** (docstring/summary embedding,
  RAPTOR-style hierarchical summarization, LLM-based entity/relationship
  graph extraction). Per [ADR-0007](adr/0007-llm-free-indexing-deferred-rag-enrichment.md),
  Phase 2's indexing job stays LLM-free by default to keep per-push indexing
  cost and latency low; these techniques land later, if ever, as an
  explicitly opt-in capability a customer configures, not a change to the
  default indexing path.
- **Dynamic/runtime verification** (executing PR-supplied code in a sandbox
  to confirm a bug is real — Greptile's TREX, research §3). Deliberately not
  pursued for now: the research doc's own CodeRabbit "PwnedRabbit" case study
  (§3, §6) documents a real sandbox-escape/key-exfiltration incident from
  exactly this class of feature (running PR-supplied content through
  execution), and review is a self-hosted product running inside a
  customer's own cluster with no shared-fleet blast-radius containment
  ([ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md)) — the risk profile
  doesn't currently justify the parity gain. Revisit only alongside a
  dedicated sandboxing/isolation design, not as an incremental add-on to an
  existing phase.

---

## How to update this roadmap

- Reorder or re-scope a phase when a real dependency changes, not to reflect
  optimism or slippage — this document sequences work, it doesn't track
  progress. Use issue tracking (Jira, GitHub Projects, whatever the team
  uses) for status; don't turn this into a Kanban board.
- When a phase's "Open" note gets resolved (an ADR lands, an open question in
  `c4-architecture.md` gets answered), remove the note — don't leave stale
  open-question callouts here once the source document has resolved them.
- A new phase gets a name, a one-line reason it's ordered where it is
  relative to its neighbors, and a short bullet list of what lands — not a
  full task breakdown. If you're writing ticket-level detail, it belongs in
  the tracker, not here.
- If a phase moves across the MVP line, update the "MVP line" callout in
  place rather than leaving two conflicting statements of what MVP includes.
- Keep the "Deferred, not scheduled" section for genuinely deliberate
  deferrals only (backed by an ADR or an explicit decision) — not a dumping
  ground for ideas nobody has committed to.
