# Plan: Phase 1 — Walking Skeleton Review Pipeline

**Status: Proposed.** Sequences [`docs/specs/pipeline-step-framework.md`](../specs/pipeline-step-framework.md)
and [`docs/specs/phase-1-review-pipeline.md`](../specs/phase-1-review-pipeline.md)
— together, [`docs/roadmap.md`](../roadmap.md)'s Phase 1 ("Walking skeleton:
the core review loop") — into buildable, independently-testable phases,
following the same phase/tests/acceptance shape
[`plan-plan-0-delivery.md`](plan-plan-0-delivery.md) established as this
repo's `docs/plans/` convention. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification
tag `RQPTVE`).

Both PRDs were themselves self-reviewed to "ready to hand to a planning
agent" against `prd-review`'s ten dimensions before this plan was drafted —
this plan does not re-litigate any product/architecture call either PRD
already made (two-level step framework; Gitleaks + Semgrep as Phase 1's two
analyzers; analyzer failures isolated/non-fatal, LLM/output-step failures
fatal; check-run conclusion always "neutral"; comments batched into one PR
review submission). It only sequences the work and, where fresh codebase
exploration surfaced something neither PRD fully specified, resolves it
directly against existing project decisions (never a silent guess — see
"Assumptions this plan makes" and the corrections called out inline).
**Three deliberate, named exceptions to that "doesn't re-litigate" claim
exist and are called out by name here rather than left for a reader to
discover only in the Assumptions section below: two genuine decision
reversals** — a per-repo enqueue rate limiter that reverses
`phase-1-review-pipeline.md`'s explicit NFR decision, and a GitHub-side
retry/backoff policy that reverses `pipeline-step-framework.md`'s own
explicit "no retry-with-backoff... not invented here" NFR text —
**and one wholly new mechanism neither PRD specifies at all:** an
LLM-output/static-analysis secret-redaction subsystem. All three are
built, not only flagged, and split into two categories that get different
treatment. **A product/cost-policy tradeoff** — one only a PRD owner
should set, since it trades a real protection against one risk for a real
cost imposed on legitimate usage — gets a genuine pre-activation gate:
built and tested, but shipped **default-disabled**, so Phase 1's own
shipped configuration matches what the PRD currently specifies until a PRD
owner (or an operator acting on their behalf) makes an affirmative,
single-config-value opt-in. The rate limiter is this plan's one instance
of that category. **Baseline engineering practice** — a mitigation for a
live vulnerability or reliability gap that belongs in any responsible
implementation regardless of roadmap sign-off — is disclosed with the same
prominence but shipped **default-active** and built directly, per this
plan's own "ordinary, reversible implementer choices... entitled to make
without blocking on the user" doctrine (below). `TextRedactor` and the
retry/backoff policy are this plan's two instances of that category. See
"Assumptions this plan makes" for the full reasoning behind each.

**On this plan's aggregate size:** a review pass raised whether the sum of
all fixes accumulated across this plan's review history still matches
"walking skeleton" scope, given the plan's own length relative to the two
source PRDs. Decision: yes — "walking skeleton" describes the pipeline's
functional shape (one bare-diff pass, no verification, no indexing), not
the absence of correctness/security guardrails discovered along the way.
Every mechanism here traces to a specific, validated gap this review
process found, not speculative hardening, so the plan's size is accepted
as-is. Not re-litigated in later passes.

Phase 0 is complete (`7201ebd`) — this is genuinely additive work on a
real, running scaffold, not greenfield. Every phase below names the
existing file/module it extends.

## Scope

**In scope** — every unit both PRDs specify:
- `pipeline-step-framework.md`'s entire scope: `Step` Protocol (two nested
  levels), concurrent execution of independent steps, per-step bootstrap
  on/off config, failure isolation, startup validation.
- `phase-1-review-pipeline.md`'s Units A–G: installation/org/repo
  resolution + idempotent enqueue, Worker job dispatch, pre-filter,
  static-analysis (Gitleaks + Semgrep), llm-review, findings-merge-and-output
  + GitHub posting client, dry-run mode.

**Out of scope** — both PRDs' own out-of-scope lists apply unchanged (real
indexing/RAG, verification pass, Memory, admin UI, multi-org RBAC, knowhow
augmentation, comment-triggered re-review, severity-based check-run
blocking — all later roadmap phases). Not repeated here in full; see each
PRD's Scope section.

**Traceability:** every user story in both PRDs (`US-F1`–`US-F7`,
`US1`–`US9`) maps to at least one integration (or unit, where the story is
pure domain logic with no I/O) test named in a phase below, per
`testing.md`'s mandatory PRD → user story → integration test rule. The
consolidated table is at the end of this plan, after Phase 7.

**Observability scope, stated once rather than left implicit across seven
per-phase subsections:** every phase's "Observability & rollback"
subsection below is structured-logs-only, extending Phase 0's own
established logging pattern — this plan adds no metrics, dashboards, or
alerting anywhere (confirmed: neither the codebase nor Phase 0's plan
mentions a `/metrics` endpoint or any metrics library). That is a
deliberate scope boundary for a walking skeleton, not an oversight: adding
real metrics infrastructure (a Prometheus client, dashboards, alert rules)
is itself new, unrequested scope beyond what either PRD asks for, the same
category this plan holds every other addition to per dimension 5's own
"smallest form that works" standard — building it here would be the
inconsistency, not the gap. The operational cost of that boundary is real
and worth naming plainly: for Phase 1, discovering a sustained failure
(webhook rejection spike, dispatch-loop stalls, GitHub API errors) means an
operator manually tailing structured logs or querying Postgres, not being
paged. Metrics/dashboards/alerting on top of this phase's structured logs
is explicit Phase 8 (production hardening) scope — named the same way
every other deferred hardening item in this plan is named, not a silent
absence.

## Assumptions this plan makes (flagged, not decided)

None of these is fixed by either PRD or any architecture doc — all are
ordinary, reversible implementer choices this plan is entitled to make
without blocking on the user, per `plan-create`'s "a plan sequences, it
doesn't decide" principle — **except the rate-limiter item below**, which
is a genuine reversal of a stated PRD NFR decision requiring a real
pre-activation gate (default-disabled, not merely reversible), not an
ordinary un-fixed choice; it's included in this section for proximity to
the plan's other PRD-facing items, not because it fits this section's own
"entitled to decide" framing. The GitHub retry/backoff policy and
`TextRedactor`, despite also being named exceptions to the "doesn't
re-litigate" claim above, are — per the
product/cost-policy-vs.-baseline-engineering-practice distinction stated
in full below — genuinely ordinary entitled-to-decide additions: disclosed
prominently, but not gated, and this section's general framing applies to
them without exception.
`phase-1-review-pipeline.md`'s two previously
open items (trigger event set, dry-run granularity) are now resolved
decisions, not assumptions — see that PRD's Scope and Rollout & Release
Safety sections; both are implemented per Phase 1 and Phase 7 below with
an eye to extensibility (a declarative trigger-mapping table; a
context-threaded dry-run flag any future side-effecting step can honor)
rather than a hardcoded shape that would need a rewrite later. The rest
below are new, surfaced by this plan's own codebase exploration.

- **Analyzer binary packaging.** Semgrep ships a proper PyPI wheel
  (`semgrep`) and is added to `pyproject.toml`'s main `dependencies` like
  any other library. Gitleaks does not — it's a Go binary with no
  first-party Python packaging. Per the existing multi-stage `Dockerfile`
  pattern (it already does `COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx
  /bin/` for `uv` itself), Gitleaks is vendored the same way: `COPY
  --from=zricethezav/gitleaks:v8.21.2 /usr/bin/gitleaks
  /usr/local/bin/gitleaks` in the Dockerfile's `runtime` (and `dev`) stage,
  pinned to an explicit tag, not `:latest`. CI's integration-test job (which
  runs `pytest -m integration` directly on the runner, not inside the built
  image) needs the same binary available separately — a
  `curl`-and-checksum-verify step against a pinned GitHub release, mirroring
  how `astral-sh/setup-uv` already pins a version in `ci.yml`.
- **Semgrep ruleset — correction to the PRD's wording.** Both PRDs describe
  Semgrep as running with an "auto/default ruleset." Fresh exploration
  during this plan surfaced that Semgrep's `--config=auto` fetches rule
  definitions from semgrep.dev's registry over the network at scan time.
  That's not a customer-code-leaves-the-building violation (only rule
  *definitions* are downloaded, never diff content or findings — codescanning
  is entirely local), but it is a live, non-deterministic, network-dependent
  behavior: findings could silently change between two scans of identical
  code as the registry's ruleset updates, and it would fail outright in an
  air-gapped customer deployment. This plan pins a specific, vendored
  ruleset (e.g. Semgrep's `p/ci` pack, fetched once at image-build time via
  `semgrep --config=p/ci --dryrun` or downloaded as a static YAML and
  `COPY`'d into the image) instead of a live `--config=auto` call at review
  time — same "broad default coverage" intent the PRDs describe, but
  deterministic and offline-safe. **Pinned by content, not only by fetch
  timing:** unlike Gitleaks (pinned to an explicit `v8.21.2` tag) and the
  Python dependency set (pinned via `uv.lock`), a ruleset name alone (`p/ci`)
  has no integrity check — two image builds from an unchanged Dockerfile
  could silently bake in different ruleset content if the upstream pack
  changes between builds, undermining the determinism this bullet exists to
  guarantee. The build step computes and pins a checksum of the fetched
  ruleset YAML the first time it's vendored, and CI verifies the checksum
  matches on every subsequent build — a mismatch fails the build, the same
  fatal-not-warn action every other pinning check in this plan takes, so a
  drifted ruleset can never silently ship in a rebuilt image. This is trust-on-first-use drift
  detection, not external integrity verification the way the Gitleaks
  binary's checksum is (that one is checked against a value published
  alongside a pinned GitHub release; semgrep.dev's `p/ci` pack has no
  equivalent published-checksum registry to verify against) — a smaller
  claim than "the same pattern," but still enough to turn a future upstream
  ruleset change into a detected, deliberate re-pin rather than a silent
  drift. Worth reflecting back into
  `phase-1-review-pipeline.md`'s wording; flagged in this plan's handoff
  rather than silently diverging from what's written there.
- **GitHub Adapter's HTTP client.** Direct `httpx` calls to GitHub's REST
  API (check-runs, PR reviews), matching the pattern already established in
  `adapter/github/manifest.py`'s `exchange_temporary_code` — no new
  GitHub-SDK dependency, per `architecture.md`'s "no interface/abstraction
  without a second real implementation already in view" (a SDK wrapping
  one already-direct `httpx` pattern used everywhere else in this adapter
  has no such second implementation in view).
- **"Its own package" read as submodule, not top-level package.**
  `architecture.md`'s Decoupling section says each pipeline stage "is its
  own package"; read literally that could mean a top-level package per
  stage. This plan follows the reading `review/__init__.py`'s own
  Phase-0-authored docstring already committed to — pre-filter, static
  analysis, LLM inference, and findings-merging all live as submodules
  *within* the one `review` package (`review/prefilter.py`,
  `review/static_analysis.py`, etc.), each still with a narrow interface
  and pydantic models between them, per the same rule's substance. Worth a
  one-line clarification in `architecture.md` itself so future phases don't
  have to re-derive this from a docstring; not blocking here since Phase
  0's own code already established the precedent this plan follows.
- **Two genuine PRD-decision reversals, and one wholly new plan-invented
  security mechanism, sorted into two categories that get different
  treatment:**
  - **Product/cost-policy tradeoffs** — decisions about acceptable spend or
    acceptable risk tolerance that are genuinely the PRD owner's to set,
    not an implementer's. The rate limiter below is this plan's one
    instance: it trades LLM cost-exhaustion protection against arbitrarily
    bounding legitimate rapid-iteration pushes, a tradeoff only a PRD owner
    can weigh. This category gets a **real pre-activation gate** — built
    and tested, but shipped **default-disabled**, so Phase 1's actual
    shipped configuration matches what the PRD currently specifies until a
    PRD owner (or an operator acting on their behalf) makes an
    affirmative, single-config-value opt-in. That is a stronger property
    than "reversible": the reversal only takes effect on confirmation, not
    by default.
  - **Baseline engineering practice** — mitigations for a live
    vulnerability, reliability gap, or correctness gap that belong in any
    responsible implementation regardless of roadmap sign-off, the same
    way input sanitization doesn't wait for product sign-off. `TextRedactor`
    (preventing secret leakage into posted PR comments) and the GitHub
    retry/backoff policy (preventing a single transient blip from failing
    an otherwise-healthy review) are both this category — disclosed
    prominently as deliberate NFR-facing additions (their own bullets
    below), shipped **default-active**, built directly per this section's
    own "ordinary, reversible implementer choices... entitled to make
    without blocking on the user" doctrine. The further-below "also worth
    reflecting back into the PRDs" list's ~8 items are consistently this
    same category too: disclosure-only, never gated.
  - **Rate limiting reverses a stated PRD decision, not a silent one** (the
    first of the three exceptions this plan's opening paragraph names
    explicitly).
    `phase-1-review-pipeline.md`'s NFR section explicitly says "no
    additional cross-review rate-limiting is specified for Phase 1 ...
    GitHub's own per-installation rate limits are the operative ceiling."
    Phase 1 adds a per-repo enqueue rate limiter anyway. That's a genuine
    reversal, not a gap the PRD's reasoning simply didn't cover — it
    should be read as this plan disagreeing with the PRD's stated NFR,
    on this specific factual basis: GitHub's rate limits bound *API call
    frequency*; they say nothing about *LLM inference cost*, and once
    `synchronize` is a trigger (a decision both PRDs did make
    deliberately), those two are fully decoupled — a PR author can drive
    unbounded LLM spend by pushing repeatedly without ever approaching a
    GitHub rate limit. This plan implements the mitigation rather than
    only flagging the disagreement, because leaving a known,
    cheaply-closeable cost-exhaustion vector unmitigated through to
    execution is a worse outcome than a small, reversible addition — but
    the PRD's NFR wording should be corrected to reflect this reasoning,
    not silently left contradicting what Phase 1 actually ships.

    `InMemoryRateLimiter` (`apps/webhook_api/rate_limiter.py`) raises
    `ValueError` for `max_requests < 1`, so no value of
    `review_enqueue_rate_limit_per_minute` passed straight into its
    constructor actually disables the mechanism — only throttles it more
    loosely. Phase 1's composition root (`apps/webhook_api/main.py`)
    therefore treats `0` as a documented sentinel meaning "skip the
    rate-check call entirely" — handled at the call site (construct no
    limiter instance, or skip invoking `allow()`, when the configured
    value is `0`), not by loosening `InMemoryRateLimiter`'s own
    `ValueError` guard, which stays exactly as strict for its existing
    per-IP use.

    `review_enqueue_rate_limit_per_minute` **defaults to `0` (disabled)**,
    not `10` — Phase 1's actual shipped configuration matches
    `phase-1-review-pipeline.md`'s current stated position of no rate
    limiting, exactly. This is a real pre-activation gate, not a "ship now,
    ask later" promise: the mechanism itself is fully built and tested (see
    this phase's own Tests subsection below), so a PRD owner who reviews
    this plan's cost-exhaustion reasoning and agrees can activate it with a
    single config value and zero code change — but nothing in Phase 1's own
    shipped defaults reverses the PRD's NFR unless and until that
    affirmative confirmation happens. A one-time `warning`-level startup
    log (`apps/webhook_api/main.py`, alongside the existing startup-check
    logging) states the per-repo rate limiter is inactive at its default
    configuration. `warning`, not `info`: the distinguishing test between
    this log and the `github_retry_max_attempts=0`/
    `secret_redaction_enabled=False` startup logs below isn't "was this a
    deliberate choice" (all three are) — it's whether the logged state is
    *this phase's own shipped default* or *an operator's deviation away
    from an active-by-default protection*. The redaction and retry logs
    fire only when an operator has actively opted out of a protection this
    phase ships active; this log fires for the state every Phase 1
    deployment starts in until someone acts, so it warrants the more
    prominent level — an operator who never reads this plan's reasoning
    should still be able to find "the cost-exhaustion mitigation is off"
    by grepping `warning`, not only by reading
    `apps/webhook_api/settings.py`'s default or an `info` line easily lost
    among routine startup noise.
  - **Secret redaction (Phase 5, reused by Phase 6) is genuinely new
    scope, not a PRD-anticipated case either PRD merely omitted wording
    for** (the third exception this plan's opening paragraph names
    explicitly, and the only one of the three that's wholly new scope
    rather than a decision reversal). Neither PRD discusses secret
    redaction of any kind — Gitleaks' `--redact` flag (Phase 5) is itself a
    detail this plan introduced, not an existing PRD decision this bullet
    can lean on as precedent. This plan builds the `TextRedactor` mechanism
    in full — a Gitleaks-backed implementation, fail-closed semantics, an
    independent `secret_redaction_enabled` bootstrap flag (decoupled from
    the Gitleaks *scanner* sub-step's own `static_analysis_gitleaks_enabled`
    toggle — see Phase 5's "Interaction with
    `static_analysis_gitleaks_enabled`" bullet), and a finding-count
    ceiling bounding the same unbounded-subprocess-spawn risk Phase 6 caps
    for its own (smaller, LLM-sourced) share of the same mechanism. Defined
    in `findings/redact.py` — the bounded-context package the `Finding`
    data model it redacts belongs to, per ADR-0006's actual rule (a
    Protocol with no data model of its own sits in the package it most
    substantively serves, not arbitrarily in one of its N>1 step-module
    consumers; see Phase 5's own bullet for why `review/static_analysis.py`
    doesn't fit). Phase 6 (LLM-review) reuses this exact mechanism rather
    than defining a second one — see Phase 6's own bullet.

    This closes a live secret-leak vector (a Gitleaks- or LLM-sourced
    finding quoting a real secret verbatim, posted permanently to the
    customer's own PR) the same way input sanitization isn't deferred
    pending product sign-off — baseline engineering practice, not a
    product/cost-policy tradeoff. It ships **default-active**, built
    directly per this plan's own "ordinary, reversible implementer
    choices... entitled to make without blocking on the user" doctrine —
    disclosed prominently here and worth reflecting back into the PRDs'
    own wording (the same "plan built it, PRD should catch up" basis as
    every item in the lesser list further below), but not held back
    pending prior approval. Phase 5's independent `secret_redaction_enabled`
    bootstrap flag still gives an operator or PRD owner who disagrees with
    this plan's reasoning a single-config-value way to turn it off after
    the fact — reversible, unlike the rate limiter, rather than pre-gated,
    since the default here is protection on, not protection withheld
    pending confirmation.
  - **The GitHub-side retry/backoff policy (Phase 4) also reverses a stated
    PRD decision, not merely fills a gap** (the second of the three
    exceptions this plan's opening paragraph names explicitly).
    `pipeline-step-framework.md`'s NFR text states plainly: "there is no
    retry-with-backoff or circuit-breaker requirement yet... automatic
    retry policy is not specified by any roadmap phase... not invented
    here." Phase 4 implements one anyway (429/Retry-After capped at 10s,
    bare-5xx exponential backoff up to 3 attempts, idempotency-aware retry
    scoping that only ever retries safe GETs and specifically-idempotent
    POSTs — see Phase 4's own bullet). That is a direct textual reversal of
    an explicit "not invented here," the same category of disagreement as
    the rate limiter's NFR reversal, not a silent gap the PRD's reasoning
    simply didn't cover. This plan ships it anyway because the alternative
    for a walking skeleton is worse: with zero retry, a fully healthy PR
    review fails outright on any single transient GitHub 5xx or rate-limit
    blip — exactly the kind of routine, non-actionable failure a bounded,
    idempotency-scoped retry exists to absorb, and the cost of getting this
    wrong (a slightly longer worst-case latency on an already-generous
    per-step timeout budget) is far smaller than the cost of the rate
    limiter's own cost-exhaustion risk.

    Unlike the rate limiter and `TextRedactor`, this mechanism has no
    single boolean config flag to disable it wholesale — it's parameterized
    (retry count, backoff ceiling) rather than binary. `WorkerSettings`
    gains `github_retry_max_attempts: int = 3` (defined alongside the
    phase's other new `WorkerSettings` fields — see the
    `github_api_base_url`/`github_api_timeout_seconds` bullet below, which
    this field is added next to); setting it to `0` (still an ordinary
    validated value, not a magic sentinel needing special handling the way
    the rate limiter's `0` does, since a bounded retry loop naturally
    no-ops at zero iterations) achieves the same effect as disabling it
    wholesale. `github_retry_max_attempts` gates both retry paths this
    helper implements — the bare-5xx exponential-backoff loop (bounded
    directly by this count) and the 429/`Retry-After` single
    wait-then-proceed behavior (treated as one bounded retry attempt for
    the purpose of this counter, not a separate, ungated mechanism). At
    `0`, neither path retries or waits: a 429/secondary-rate-limit response
    and a bare 5xx are both surfaced as fatal on the first response,
    identical in shape to how a permanent error (401/403/404) is already
    handled below.

    Unlike the rate limiter, this isn't a tradeoff between two
    stakeholders' interests — it's a reliability floor (a fully healthy PR
    review shouldn't fail outright on one transient GitHub blip) that any
    responsible implementation includes regardless of roadmap sign-off, so
    it's disclosed but not gated: it ships **default-active**
    (`github_retry_max_attempts=3`) and built directly, not held back
    pending prior approval. `pipeline-step-framework.md`'s Reliability NFR
    is now corrected directly (the same treatment its US-F6 clause gets in
    Phase 7) to distinguish the job-level "no automatic re-enqueue of a
    `FAILED` job" contract it actually states from this call-level retry of
    individual GitHub requests within one step's own execution, which
    doesn't touch that contract. If an operator or PRD owner sets
    `github_retry_max_attempts=0` after reading this reasoning and
    disagreeing with it, a one-time `info`-level startup log states GitHub
    API retries are disabled — `info`, **not** the rate limiter's own
    default-state log above (`warning`): the rate limiter's `warning` fires
    for *this phase's own shipped default* (a vector left open with no
    operator action at all), while this log fires only when an operator
    has actively deviated *away* from an active-by-default protection —
    the same "shipped default vs. deliberate deviation" distinction the
    rate limiter's own bullet states explicitly. A second, independent axis
    distinguishes this log from `secret_redaction_enabled=False`'s own
    `warning` (Phase 5), which is also an active-protection opt-out: among
    opt-outs from an active-by-default protection, the level still tracks
    the severity of what's being turned off. Disabling redaction reopens a
    live secret-leak vector into a permanently-posted PR comment —
    `warning`. Disabling retries only removes an absorption layer for
    ordinary, otherwise-survivable transient GitHub hiccups, worse latency
    and an occasional avoidable job failure, not a leak — `info` is
    proportionate. Both axes — shipped-default-vs-deviation, and
    severity-of-consequence among deviations — are what set this plan's
    three startup-log levels, not a single "visible vs. silent" test that
    would flatten all three to the same level.
  - **Also worth reflecting back into the PRDs**, on the same "plan built
    it, PRD should catch up" basis, though none of these are decision
    reversals — a lesser category than the three named exceptions above,
    not a fourth instance of them. Per the
    product/cost-policy-vs.-baseline-engineering-practice distinction
    stated at the top of this bullet cluster, every item below is baseline
    engineering practice, the same category as `TextRedactor` and the
    retry/backoff policy, not the rate limiter's: disclosure-only, never
    gated behind prior PRD-owner confirmation, consistently. The diff-fetch
    mechanism (Files-endpoint pagination + Git-Trees-API for real file mode
    + per-surviving-file Git-Blobs-API content, replacing the PRD's implied
    single-call "bare diff" fetch), the path-traversal/symlink/submodule
    materialization guards (neither PRD mentions file mode, symlinks, or
    path traversal at all), the concrete container resource-limit figures
    (closing a gap where `pipeline-step-framework.md`'s NFR text implies
    real limits already exist when Phase 0 actually left them at `{}`), the
    `llm_max_diff_bytes` prompt-size cap, the redaction finding-count
    ceilings (Phase 5's 50-finding static-analysis cap and Phase 6's
    20-finding LLM cap — both silently-dropping, product-visible caps
    neither PRD specifies), the 500-file and 200MB aggregate-bytes
    materialization ceilings (Phase 4), and the posted-findings ceiling
    (Phase 7) — same category throughout. Phase 3's stuck-job warning
    mechanism (a wait-loop around the job's own execution plus one new
    config field, `stuck_job_warning_threshold_seconds`) is the
    lowest-stakes of this group but still operator-visible behavior
    invented rather than requested.

    The per-step timeout mechanism itself does NOT belong in this list:
    `pipeline-step-framework.md`'s NFR text specifies both the requirement
    (US-F5/US-F6's "exceeds its timeout" Given/When/Then clauses) and the
    exact shape Phase 2 implements — "each step declares its own timeout
    with a default of 30s ... overridable per step via the same
    bootstrap-config mechanism as its on/off flag." Phase 2's
    `StepTimeouts` model is that shape, not a deviation from it. The one
    implementer judgment call within that PRD-specified shape — which
    specific steps' defaults are overridden away from 30s, and to what
    value — is an ordinary, reversible per-step-config choice the PRD
    explicitly delegates to whoever builds it ("overridable per step"), not
    a second PRD gap this plan is flagging back. See Phase 2's own bullet
    for the reasoning behind the static-analysis and llm-review steps'
    larger defaults.

## Correction surfaced during exploration (not an assumption — a factual gap in both PRDs)

Both PRDs' NFR sections state the new GitHub write capability "reuses the
existing GitHub App installation-token exchange." That capability **does
not exist yet.** `adapter/github/manifest.py`'s `exchange_temporary_code`
is the one-time, operator-driven *App-creation* code exchange; it hands
credentials back to the operator and persists nothing (`.env.example`'s own
comment: "nothing in Phase 0 authenticates to the GitHub API with them yet
... a later roadmap phase that adds real GitHub API calls under the App's
identity introduces the corresponding settings field ... then, not
speculatively here"). This phase's Unit G *is* that later phase — it needs
new, genuinely additive work: signing a JWT with the App's private key and
exchanging it for a short-lived installation access token
(`POST /app/installations/{id}/access_tokens`), the standard GitHub App
server-to-server auth flow. Phase 0 did leave the groundwork for this:
`apps/webhook_api/manifest_routes.py`'s `_build_app_manifest` already
requests exactly the permissions this needs (`checks: write`,
`pull_requests: write`, `contents: read`, `metadata: read`) and
`default_events: ["pull_request", "push"]` — no manifest/App-registration
change required, only new adapter code. See Phase 7 below.

## Phase 1 — Installation/Org/Repo resolution + idempotent enqueue

**Why first:** fully independent of every other phase (no shared code with
the step framework or any pipeline step) — the PRD's own Unit A has no
dependency. Building it first also closes the exact gap Phase 0's own plan
flagged forward in its Security review: *"Webhook replay. Not addressed in
Phase 0 ... Phase 1 ... should dedupe on GitHub's `X-GitHub-Delivery`
header before this becomes a real vector."*

- **Correction to an over-hasty first draft of this bullet (architecture
  boundary):** installation/Org/Repo resolution and delivery-idempotency
  are domain rules ("what does it mean to see this installation for the
  first time," "has this delivery already been processed"), not entrypoint
  plumbing — `tenancy/__init__.py` already exists with exactly this
  docstring ("org/repo/team/installation domain model and multi-org
  isolation rules") and had nothing built in it yet. Putting this logic
  inline in `apps/webhook_api/webhook_routes.py` would organize by layer,
  not bounded context, the one thing `architecture.md` calls out first.
  New `tenancy/resolution.py`: a `TenancyStore` `typing.Protocol` (defined
  here, the consumer, per `architecture.md` — `get_or_create_org_repo(...)`,
  `record_delivery_if_new(delivery_id: str, *, unit_of_work: object | None
  = None) -> bool`) plus a plain function, `resolve_installation(event:
  WebhookEvent, store: TenancyStore, *, unit_of_work: object | None =
  None) -> ResolutionResult`, holding the actual business rules
  (auto-provision on first sight, dedupe by delivery id). **`unit_of_work`,
  typed `object` rather than `sqlalchemy.ext.asyncio.AsyncSession`, so
  this domain-defined Protocol stays free of the infra import that type
  would otherwise force — see "Transaction boundary" below for why this
  parameter exists and how the concrete adapter uses it:** `resolve_installation`
  never inspects the value, only relays it through to `store`'s call
  unchanged, so the domain function's own zero-infra-imports property
  holds regardless of what the entrypoint actually passes. **Domain-owned input type, not the
  adapter's envelope — a violation this plan's own review caught in an
  earlier draft, which had `resolve_installation` accept
  `adapter/github/schemas.py`'s `GitHubWebhookEnvelope` directly:** that
  would force `tenancy` (a domain package) to import from `adapter/github`
  to type-check under `mypy --strict`, the exact "domain never imports
  adapter code" violation this bullet's own opening paragraph was trying
  to avoid, and it would also mean a future second git-host adapter (e.g.
  GitLab) couldn't call into this same resolution logic without either a
  second, near-duplicate `resolve_installation` or `tenancy/resolution.py`
  branching on which concrete envelope type it received — both violating
  "new adapter = zero domain changes." Fixed by defining a small,
  domain-owned pydantic model directly in `tenancy/resolution.py`,
  `WebhookEvent` (`delivery_id: str`, `installation_id`, `action: str | None`,
  `repo_full_name`, `pr_number: int | None`, `head_sha: str | None` —
  exactly the fields `resolve_installation` actually needs, nothing
  GitHub-specific in name or shape), and having `resolve_installation`
  accept that instead. `action` is optional here for the same reason it's
  optional on `GitHubWebhookEnvelope` below: a real `push` delivery has no
  top-level `action` key, and `triggers.py`'s frozenset-membership lookup
  already treats `None` as "no match, accept, don't enqueue" — a required
  field here would reject every `push` webhook one layer downstream of the
  fix to the envelope itself. **`delivery_id`, corrected into the field list by
  this pass — an earlier draft of this fix carried over every field
  `resolve_installation`'s prose already described except the one its own
  dedupe logic actually depends on:** without it, `resolve_installation`
  has no way to call `TenancyStore.record_delivery_if_new(...)` at all,
  silently breaking the exact anti-replay mechanism this phase exists to
  build (see this phase's own "Why first" framing above). Sourced from
  GitHub's `X-GitHub-Delivery` header, which the existing route already
  reads for logging (unchanged by this fix).
  `apps/webhook_api/webhook_routes.py` (the entrypoint, which is allowed to
  depend on adapters) constructs a `WebhookEvent` from the already-parsed
  `GitHubWebhookEnvelope` and the request's `X-GitHub-Delivery` header
  immediately before calling into `tenancy.resolution`, so the
  adapter-to-domain translation happens once, at the boundary, rather than
  inside the domain package itself.
  `adapter/storage`'s new repository method (below)
  implements `TenancyStore`; `apps/webhook_api/webhook_routes.py` calls
  only `tenancy.resolution.resolve_installation`, with the concrete
  `adapter/storage` implementation wired in at the composition root
  (`apps/webhook_api/main.py`), matching the Protocol-boundary pattern
  used everywhere else in this plan (`StaticAnalyzer`, `LLMClient`,
  `DiffFetcher`, `FindingsPublisher`).
- Extends `apps/webhook_api/webhook_routes.py` (currently: verifies
  signature, parses `GitHubWebhookEnvelope`, logs "webhook accepted",
  enqueues nothing — see its own docstring). Add, after signature
  verification and payload validation, in order:
  1. Construct a `WebhookEvent` (above) from the parsed
     `GitHubWebhookEnvelope` and the request's `X-GitHub-Delivery` header,
     then call `tenancy.resolution.resolve_installation(event, store)`
     (above), which auto-provisions Org/Repo on first sight (an
     `installation_id` column is a new addition to the existing `Repo`
     model in `adapter/storage/models.py` — Phase 0's `Repo` has no field
     recording which GitHub installation it belongs to yet, since nothing
     needed that mapping until now) and records the delivery against a new
     `webhook_delivery` table (id = `event.delivery_id`, above) — a `UNIQUE`
     constraint, insert-or-detect-conflict, satisfying US2's idempotency
     requirement without a second round-trip. **This step runs for every
     accepted webhook, regardless of trigger-set membership (below) —
     ordering made explicit here, corrected from an earlier draft that left
     it ambiguous whether a non-triggering action skips this step:** the
     anti-replay/idempotency guarantee has to hold for all traffic, not
     only trigger-matched `pull_request` events, matching this phase's own
     statement below that `webhook_delivery` records "every
     uniquely-delivered webhook... including ones later rejected by the
     per-repo rate limiter." The `(event_type, action)` trigger-set lookup
     is evaluated first, before this step — cheap, pure, no I/O — purely to
     decide the session strategy below (whether a shared session needs
     opening at all); step 1's own recorded behavior (the delivery insert)
     stays unconditional regardless of the lookup's result. A non-matching
     action is accepted (200) and logged, with no rate check or enqueue
     attempted. **Duplicate-conflict detection, uniform regardless of
     session sharing:** `PostgresTenancyStore.record_delivery_if_new`
     inserts with Postgres's native `INSERT ... ON CONFLICT DO NOTHING`
     (`sqlalchemy.dialects.postgresql.insert(WebhookDeliveryRow).values(id=delivery_id,
     ...).on_conflict_do_nothing(index_elements=["id"]).returning(WebhookDeliveryRow.id)`
     — `adapter/storage/models.py` already imports from
     `sqlalchemy.dialects.postgresql` for `JSONB`, so this is no new
     dependency), returning `True` if a row came back and `False` if the
     conflict produced none — satisfying the `TenancyStore` Protocol's
     declared `-> bool` contract identically whether or not the call is
     running inside the enqueuing path's shared session (see "Transaction
     boundary" below). A genuine redelivery of an already-seen,
     trigger-matched webhook is simply a no-op insert: no exception is ever
     raised, so there is nothing for `resolve_installation` or the route to
     catch, and the outer shared transaction (when one is open) is never at
     risk of being poisoned by it — this also keeps the adapter honoring
     this codebase's own convention that a raw driver exception
     (`sqlalchemy.exc.IntegrityError`) never crosses the adapter boundary
     uncaught (`adapter/storage/exceptions.py`), which an earlier draft of
     this mechanism (a `SAVEPOINT`-plus-catch approach) would have violated
     on this exact path. **No retention/pruning
     policy, flagged rather than silently unbounded:** unlike every
     capped-and-logged ceiling elsewhere in this plan, this table has no
     equivalent bound — every uniquely-delivered webhook inserts a
     permanent row, including ones later rejected by the per-repo rate
     limiter. Unbounded Postgres growth over time; a scheduled pruning job
     for delivery rows older than some retention window (e.g. 90 days,
     comfortably past any plausible GitHub redelivery window) is Phase 8
     operational hardening, not built here. **No allow-list/approval gate on
     auto-provisioning, accepted for Phase 1 rather than silently unaddressed:**
     any GitHub account able to install the operator's App gets its
     Org/Repo auto-provisioned and a live pipeline the moment a covered
     event arrives, gated only by webhook-signature validity (App-wide, not
     per-installation). This is intentional — the operator's own GitHub App
     install flow (who is permitted to install this App against which
     account/org) is itself the access-control boundary this platform
     relies on; this plan doesn't add a second, redundant one at the
     webhook layer, consistent with a self-hosted platform where the
     operator already controls App installation.
  2. A per-repo rate check (see the new Assumption below) before
     enqueueing.
  3. `JobQueueRepository.enqueue()` (`adapter/storage/queue.py`) with
     `job_type=JobType.REVIEW` and a payload carrying org/repo id, PR
     number, head SHA, and the installation id — on this path, called
     with the same session opened for step 1's delivery-record insert
     (see "Transaction boundary" below); every other caller keeps calling
     it with no session argument and gets today's self-contained
     behavior, unchanged.
- **Transaction boundary for the enqueuing path:** when the trigger-set
  lookup and rate check both pass, step 1's `webhook_delivery` insert and
  step 3's `job_queue` insert commit inside one database transaction — a
  single commit covering both writes. **Mechanism, kept out of the domain
  layer:** `JobQueueRepository.enqueue()` (`adapter/storage/queue.py`, an
  adapter — free to reference SQLAlchemy types directly) gains one
  additive parameter, `session: AsyncSession | None = None`. Omitted —
  every call site today, including `claim_next()`'s own, unrelated calls
  elsewhere in the Worker — `enqueue()` keeps opening and committing its
  own session exactly as now, no behavior change. On the enqueuing path
  only, `apps/webhook_api/webhook_routes.py` (the entrypoint, allowed to
  hold concrete adapter types) opens one `AsyncSession` via the shared
  `session_factory` (the same `async_sessionmaker` `adapter/storage`'s
  other repositories already take) and passes it as
  `unit_of_work=session` into `resolve_installation(...)` (above) — which
  only relays it, opaquely, into `store.record_delivery_if_new(delivery_id,
  unit_of_work=session)`. The concrete `TenancyStore` implementation (in
  `adapter/storage`, also free to reference SQLAlchemy types) treats a
  non-`None` `unit_of_work` as the `AsyncSession` it actually is and uses
  it directly instead of opening its own; `resolve_installation` and the
  `TenancyStore` Protocol it's typed against never import or inspect
  `AsyncSession` itself, so the domain package's zero-infra-imports
  property this same phase's own Acceptance check proves stays intact.
  The entrypoint then passes that identical session to `enqueue()` and
  commits once after both calls succeed — a small, additive signature
  change to two existing methods, not a rewrite, and the only place
  either repository is called with an externally-supplied session. A
  mid-request failure (Postgres
  blip, unhandled exception, pod eviction) between the two rolls both back
  together, so GitHub's own retry of that same delivery is processed
  cleanly from scratch rather than being silently treated as an
  already-seen duplicate with no job ever having been enqueued — the two
  writes committing independently would otherwise create a permanent,
  operator-invisible review-suppression bug (the delivery is durably
  marked "seen" but nothing was ever claimed, and no error, no `FAILED`
  job, and no log line exists to surface it). **Logged, not just
  safe:** if the shared transaction rolls back, the route logs at
  `warning` (self-healing via GitHub's own clean retry of the same
  delivery, not a confirmed defect — closer to this plan's other
  absorbed-and-retried cases than an `error`) with the delivery id and
  installation id, so an operator can still notice a transient failure
  occurred even though nothing durably broke. On the non-enqueuing path
  (rejected by the trigger-set lookup or the rate check), only the
  delivery-record insert happens, committed on its own, with no session
  argument — there's no second write to lose, so no shared
  session/transaction is needed on that path.
- **Composition-root wiring for storage, named explicitly rather than left
  implicit:** the `TenancyStore` Protocol above is implemented by a new
  `PostgresTenancyStore` class in `adapter/storage/tenancy.py` (mirroring
  `JobQueueRepository`'s own placement in `adapter/storage/queue.py`). Its
  `record_delivery_if_new`/`get_or_create_org_repo` methods narrow the
  Protocol's opaque `unit_of_work: object | None` parameter back to the
  concrete `sqlalchemy.ext.asyncio.AsyncSession` it actually is via an
  explicit, commented `typing.cast(AsyncSession, unit_of_work)` when
  non-`None` — `# unit_of_work is always an AsyncSession when set; the
  domain Protocol types it as object only to stay free of this import` —
  not a silent, unexplained `Any`. `WebhookAPISettings`
  (`apps/webhook_api/settings.py`) gains one new required field,
  `database_url: SecretStr`, sourced from the bare `REVIEW_DATABASE_URL`
  env var rather than this class's own `REVIEW_WEBHOOK_API_` prefix, via
  `pydantic.Field(validation_alias="REVIEW_DATABASE_URL")` — the same
  variable name Phase 0's `migrate` Compose service and both Helm
  Deployments already inject, but that neither `WebhookAPISettings` nor
  `WorkerSettings` has a matching field for yet (`docker-compose.yml`'s
  `webhook_api` service block also needs `REVIEW_DATABASE_URL` added to
  its own `environment:` section — today only `migrate`'s block has it;
  Phase 3 below does the same for `worker`'s). `apps/webhook_api/main.py`'s
  `create_app` calls `create_storage_engine(settings.database_url.get_secret_value())`
  and `create_session_factory(engine)` (both already implemented,
  `adapter/storage/engine.py`, unchanged) once at startup, constructs
  `PostgresTenancyStore(session_factory)`, and passes both it and
  `session_factory` into `create_webhook_router(...)` alongside that
  factory's existing constructor arguments — the same "wire concrete
  implementations only at the composition root" pattern this plan already
  follows for every other adapter (`StaticAnalyzer`, `LLMClient`,
  `DiffFetcher`, `FindingsPublisher`). `create_storage_engine`/
  `create_session_factory` (`adapter/storage/engine.py`) are non-connecting,
  synchronous factory calls — they build an `AsyncEngine`/session factory
  without performing any I/O, so they cannot themselves fail for
  "unreachable Postgres"; only a malformed DSN string raises there, from
  `sqlalchemy.engine.make_url`'s own parsing, before any connection is
  attempted. `adapter/storage/pgvector_check.py`'s `check_pgvector_installed`
  is genuinely extended this phase to catch a connection failure at
  `engine.connect()` (today it only wraps the "extension not installed"
  case, which requires a connection to have already succeeded) and re-raise
  it as a new `StorageConnectionError(dsn_summary=redact_dsn(...))`
  (`adapter/storage/exceptions.py`) — `adapter/storage/startup_check_cli.py`'s
  `main()` (the `migrate` Compose service's second half) catches this
  alongside `PgVectorNotInstalledError` the same way. That check runs once,
  at Compose/Helm migration-gate time, against the same database the
  Webhook API and Worker then connect to — it's the one place a real
  Postgres-unreachable failure is already redacted end to end. **Known,
  accepted gap, not silently assumed solved:** a Postgres outage that
  begins *after* that gate passes — so the Webhook API's or Worker's own
  first real query (`resolve_installation`, `claim_next`) is what actually
  hits the failed connection — surfaces as whatever raw exception
  SQLAlchemy/asyncpg raises at that call site, not a `redact_dsn`-wrapped
  one; low practical severity, since neither driver's connection-failure
  exception text embeds the plaintext password, but this plan doesn't wrap
  every downstream query site the way it wraps the one startup gate — the
  same category of gap as the already-disclosed Postgres-outage-during-
  webhook-receipt 500 in "Compatibility & rollout," not a new one.
- New Alembic migration for the `installation_id` column (nullable, no
  default — see "Compatibility & rollout" for why) and the
  `webhook_delivery` table — the first migration since Phase 0's baseline,
  proving the "every phase adds a migration, never edits the baseline"
  discipline Phase 0's plan established actually holds.
- `GitHubWebhookEnvelope` (`adapter/github/schemas.py`) currently models
  only `installation.id`. Extend it (still `extra="ignore"`, still minimal)
  with `repository.full_name` and **optional** `action`/
  `pull_request.number`/`pull_request.head.sha` fields this phase's
  enqueue payload needs — the first real use of "every other event-specific
  field ... ignored" no longer being fully true, per that model's own
  docstring caveat. All three of `action`, `pull_request.number`, and
  `pull_request.head.sha` are `Optional`/default `None`, not required: the
  App's manifest already subscribes to `push` events too
  (`apps/webhook_api/manifest_routes.py`'s
  `default_events: ["pull_request", "push"]`), and a real `push` payload
  has no top-level `action` key at all (only certain event types like
  `pull_request`/`issues`/`check_run` carry one) and no `pull_request`
  object either — a required field of either kind would reject every real
  `push` delivery with a 422 before it ever reaches `triggers.py`'s lookup,
  contradicting this same phase's "every non-enqueuing outcome is still
  accepted (200)" design one bullet below. The enqueue path only runs for
  the `(event_type, action)` pairs mapped in `triggers.py` below, all of
  which are `pull_request` events where these fields are always present in
  practice — validated as present at the point of use (enqueue-payload
  construction), not by the envelope schema itself. **Failure behavior for
  that validation:** a payload for a mapped `(event_type, action)`
  pair that is nonetheless missing `pull_request.number` or
  `pull_request.head.sha` — a malformed or adversarially-crafted delivery,
  since a genuine GitHub-originated `pull_request` event always carries
  both — is treated exactly like every other non-enqueuing outcome this
  phase defines: accepted (200), not enqueued, logged at `info` with
  `skipped_reason="missing_required_fields"` and the delivery id, never a
  4xx rejection. A 4xx here would both contradict this phase's own
  "every non-enqueuing outcome is still accepted" design (stated below) and
  hand an attacker a cheap oracle for probing which malformed shapes the
  route rejects outright versus silently drops.
- New `adapter/github/triggers.py`: a small, declarative
  `frozenset[tuple[event_type, action]]` membership set — **simplified from
  an earlier draft's `dict[..., TriggerDecision]` mapping, caught by this
  plan's own review as unnecessary machinery:** Phase 1 populates it with
  `("pull_request", "opened")`, `("pull_request", "synchronize")`, and
  `("pull_request", "reopened")`, all three resolving to the identical
  outcome (enqueue a review) — a `dict` keyed to a `TriggerDecision` value
  type buys nothing when every populated entry maps to the same decision;
  a plain membership check is the PRD's required "declarative, not inline
  conditionals" shape with no unused type. Every other `(event_type,
  action)` combination is absent from the set and resolves to "accept,
  don't enqueue" by default. `webhook_routes.py` calls one lookup function
  against this set rather than branching on `action` inline — so a later
  trigger (a comment-triggered re-review, a different event type) is a new
  set entry in `triggers.py`, with zero change to the route handler itself;
  if a future trigger needs a genuinely different outcome than "enqueue a
  review" (not just a different event/action pair), that's the point at
  which `TriggerDecision` earns its keep and the set becomes a dict again —
  not before. Every non-enqueuing outcome is still accepted (200) and
  logged at `info` with the reason, matching the existing rejection-logging
  pattern. **Placement, confirmed rather than left as an unaddressed
  inconsistency with this same phase's `tenancy/resolution.py` move above:**
  unlike installation/org/repo resolution and delivery-idempotency (moved
  into `tenancy/` because "what does it mean to see this installation for
  the first time" is a domain rule), this lookup table stays in
  `adapter/github` deliberately — it's GitHub-vocabulary-specific
  (`pull_request`/`synchronize`/`reopened` are GitHub's own event/action
  names, not a git-host-neutral concept), and keeping it adapter-side means
  a future GitLab adapter defines its own trigger vocabulary in its own
  `adapter/gitlab/triggers.py` without the domain package needing to know
  either host's event naming — the same "new adapter = zero domain changes"
  boundary rule `architecture.md`'s Decoupling section states directly.
- **New mitigation, reversing a stated PRD NFR decision (added during
  this plan's own review — see "Assumptions this plan makes" for the full
  framing, deliberately not called an ordinary assumption):**
  `synchronize` is in the trigger set, and any contributor able to push
  commits to a PR on a covered repo can trigger it — with no dedup beyond
  exact-delivery-ID and no per-installation limiter, an attacker-controlled
  PR can push repeatedly to enqueue unbounded `REVIEW` jobs, each costing
  one LLM call and one GitHub write (cost exhaustion, and queue starvation
  of every other installation sharing the same Worker). This plan closes
  it the same way `apps/webhook_api/rate_limiter.py`'s existing
  `InMemoryRateLimiter` already closes the equivalent per-IP HTTP case: a
  second `InMemoryRateLimiter` instance, keyed by `repo_id` instead of
  client IP, constructed alongside the existing per-IP limiter at the same
  composition root (`apps/webhook_api/main.py`) and living in
  `apps/webhook_api/settings.py`'s `WebhookAPISettings` — the exact class
  that already holds the analogous `webhook_rate_limit_max_requests`/
  `webhook_rate_limit_window_seconds` fields this new setting extends the
  same pattern for, not `WorkerSettings` (which owns no code path in this
  route). New field `review_enqueue_rate_limit_per_minute`, default `0`
  — **disabled by default, per this plan's pre-activation-gate policy for
  genuine product/cost-policy tradeoffs (see "Assumptions this plan
  makes")**, so Phase 1's own shipped configuration imposes no rate
  limiting, matching `phase-1-review-pipeline.md`'s current NFR text
  exactly. The composition root (`apps/webhook_api/main.py`) treats `0` as
  a documented sentinel: no `InMemoryRateLimiter` instance is constructed
  for this purpose and the enqueue path skips the rate-check call entirely
  when the configured value is `0`, rather than constructing a limiter and
  relying on its own `ValueError` guard (which still applies exactly as
  before to any explicit non-zero value, including the existing per-IP
  limiter's own field). A PRD owner who reviews the reasoning below and
  wants to activate it sets this to a positive value — `10` remains this
  plan's own recommended starting point: generous for genuine
  rapid-iteration pushes on one PR, low enough to bound worst-case per-repo
  LLM spend from a single misbehaving PR, once activated.
  **Known tradeoff of per-repo rather than per-PR keying, flagged rather
  than silently accepted:** the budget is shared across every open PR on a
  repo, so one attacker-controlled PR pushing repeatedly can exhaust the
  whole repo's enqueue budget and deny review to every other legitimate PR
  on the same repo, not only bound the attacker's own cost. Per-PR keying
  would need PR-number-aware limiter state this phase doesn't otherwise
  carry (the existing per-IP limiter this pattern reuses has no such
  precedent either); a reasonable Phase 8 refinement if shared-budget
  starvation proves a real problem in practice, not a gap worth a new
  mechanism at this phase's scope. A
  request over the cap is still accepted (200) and logged at `info` with
  `skipped_reason="rate_limited"`, matching the existing rejection-logging
  pattern — never a 429 back to GitHub, which would just trigger GitHub's
  own retry. **Known limitation, inherited from the pattern being reused,
  not new to it:** `InMemoryRateLimiter`'s own docstring already flags
  it as per-process, not safe across multiple replicas — at
  `webhookApi.replicaCount > 1` (Helm defaults to `1`; nothing prevents an
  operator raising it for HA), each pod keeps an independent counter, so
  the effective cap is multiplied by pod count. Acceptable for this
  phase's scope (matching the existing per-IP limiter's own documented
  limitation exactly), not a new gap this addition introduces; a shared
  limiter for multi-replica deployments is Phase 8 hardening, same as the
  existing per-IP limiter's own deferred fix. **Unbounded `_hits` dict
  growth, flagged the same way this plan flags every other unbounded-growth
  gap — not new to `InMemoryRateLimiter`, but not previously named for this
  specific instance either:** `InMemoryRateLimiter`'s internal `_hits: dict[str,
  deque[float]]` never evicts a key once created, even after all its
  timestamps age out of the configured window — a long-lived Webhook API
  process accumulates one permanent entry per distinct `repo_id` it has
  ever rate-checked (this per-repo instance) or client IP (the existing
  per-IP instance, the same underlying gap, inherited unchanged). In-memory
  and reset on every restart/redeploy, not a persistent artifact like
  `webhook_delivery`'s Postgres growth, but the same class of gap; deferred
  to the same Phase 8 pruning/hardening bucket, not addressed in this
  phase.
- **ADR creation moved here from Phase 2 — sequencing correction, caught by
  this plan's own review:** `adapter/github/triggers.py`'s lookup table
  (above) is the first case in this plan needing Unit-tier coverage for
  pure, no-I/O logic embedded in a package `testing.md` otherwise assigns
  wholesale to the Integration tier — `testing.md`'s story→integration-test
  rule states any deviation "requires a new ADR, not a one-off exception."
  An earlier draft of this plan had Phase 2 create
  `docs/adr/0005-pure-no-io-logic-unit-tested.md`, which would mean Phase
  1's own Tests subsection (below) cites and relies on an ADR that doesn't
  exist yet at the point Phase 1 merges — this phase's PR therefore creates
  `docs/adr/0005-pure-no-io-logic-unit-tested.md` itself, scoped at this
  point to the one case Phase 1 actually needs (`triggers.py`). Phase 2
  below extends that same ADR's scope (not a second ADR) to cover its own
  two additional cases once they exist, rather than re-creating it — see
  Phase 2's own ADR bullet, corrected to reflect this.

**Tests:** Integration (in `apps/webhook_api/test_webhook_routes.py`, real
Postgres via `testcontainers-python`, per `testing.md`'s middle tier —
this is the first phase whose test needs a real DB write, not just a real
DB *read*): a verified `opened` webhook for
an unseen installation creates `Org`/`Repo` rows and enqueues one job with
the right payload (US1); a second `opened`/`synchronize` webhook for an
*already-known* installation resolves the existing `Org`/`Repo` rows
without re-provisioning and enqueues correctly (the repeat-case branch of
US1's resolution logic, not just first-sight auto-provisioning); the same
delivery ID, for a trigger-matched `pull_request` action, processed twice
yields exactly one job (US2) — this exercises `record_delivery_if_new`'s
`ON CONFLICT DO NOTHING` path specifically under the enqueuing path's
shared session (the second delivery's trigger-set match means a shared
session is opened before the duplicate is detected), proving the
return-value contract (`True` then `False`) directly rather than an
internal rollback mechanism, and that no error surfaces and no second job
is ever enqueued; an `action`
outside the trigger set is accepted but enqueues nothing; a `push` event
(no top-level `action` key and no `pull_request` object, matching a real
GitHub `push` payload) is accepted (200) and enqueues nothing, proving the
envelope's optional fields don't reject real non-PR traffic; a
`pull_request`/`opened` payload with a `pull_request` object present but
missing `number` (and, in a second case, missing `head.sha`) is accepted
(200), not enqueued, and logged with
`skipped_reason="missing_required_fields"` — proving the "point of use"
validation and its accept-don't-4xx behavior rather than leaving it as an
unverified claim; enqueues
past the per-repo rate limit are accepted but not enqueued, logged with
`skipped_reason="rate_limited"` (boundary case: the request that lands
exactly at the configured limit still enqueues; the next one, one over,
does not) — this case configures the test app with a non-zero
`review_enqueue_rate_limit_per_minute` explicitly, since Phase 1's own
shipped default is `0`; a second, default-configuration case asserts that
with the app running at its actual shipped default (`0`), an arbitrarily
large burst of enqueue-eligible webhooks for the same repo all enqueue
successfully with no `rate_limited` skip ever logged, proving the
disabled-by-default claim rather than leaving it as an unverified claim in
the Assumptions section; a case that forces an exception after the delivery-record insert has been
staged on the shared session but before the outer commit (patching
`JobQueueRepository.enqueue()`, called with that same shared session, to
raise once its row has been added) asserts neither row exists in Postgres
afterward and the `warning` log fires with the delivery id — proving the
transaction boundary above both rolls both writes back together, rather
than leaving the delivery durably marked "seen" with no job ever
enqueued, and surfaces the failure rather than swallowing it. A new `apps/webhook_api/test_main.py`
(composition-root wiring, mirroring Phase 5's `apps/worker/test_main.py`
convention for testing conditional startup logging) asserts the startup
log's *level*, not just the enqueue behavior above: at the shipped default
(`0`), the startup log fires exactly once at `warning`; with
`review_enqueue_rate_limit_per_minute` explicitly set above `0`, it fires
exactly once at `info` instead — locking in the level distinction the
Assumptions section's reasoning depends on, since this exact level was
already corrected once during this plan's own review and would otherwise
regress silently. Unit (per `docs/adr/0005-pure-no-io-logic-unit-tested.md`,
Phase 2 — a pure, no-I/O lookup table tested at Unit tier despite living
inside `adapter/github`): `adapter/github/triggers.py`'s
membership set is tested directly in
`adapter/github/test_triggers.py` — every mapped `(event_type, action)`
is a member (triggers enqueue), everything else is not (no trigger),
including a case asserting a hypothetical new entry can be added to the
set without touching the lookup function itself. The
envelope-to-enqueue-payload mapping is tested the same way, with fakes,
per `testing.md`'s "no real I/O" unit-tier rule. `tenancy/resolution.py`'s
own business rules get a dedicated Unit test too, in
`tenancy/test_resolution.py`, constructing `WebhookEvent` instances
directly, `delivery_id` included (never `GitHubWebhookEnvelope`, proving
the domain package's test suite itself needs no adapter import) against a
fake `TenancyStore` (per `testing.md`'s "new domain logic a story touches
still needs unit tests" rule — `tenancy` is one of `testing.md`'s
explicitly-listed Unit-tier domain packages): auto-provision-on-first-sight
returns a freshly-created `Org`/`Repo`; a known installation returns the
existing rows without calling the store's create path; two `WebhookEvent`s
sharing the same `delivery_id` are detected as a duplicate via the fake
store's `record_delivery_if_new(...)` without a second "provision" call. These are the same
scenarios the Phase 1 Integration tests above prove end-to-end through a
real Postgres — the Unit tier proves the decision logic itself in
isolation, per testing.md's "necessary, not sufficient" framing for
domain logic a story touches.

**Observability & rollback:** Every enqueue (and every accepted-but-not-enqueued
event) logs at `info` with `installation_id`, `org_id`/`repo_id`,
`pr_number`, and `job_id`/`skipped_reason` — extending the existing
webhook-route logging pattern, not introducing a new one. A one-time
startup log (`apps/webhook_api/main.py`, alongside the existing
startup-check logging) states whether the per-repo rate limiter is active
or inactive at its configured value — at `warning` when inactive (this
phase's own shipped default leaves a known cost-exhaustion vector open;
see the "New mitigation" bullet above for why this level, not `info`, is
correct here), `info` when an operator has actively raised it above `0`,
so an operator or PRD owner can confirm the deployment's actual state
without reading config files either way. A rollback of the enqueuing
path's shared transaction (see "Transaction boundary" above) logs at
`warning` with `delivery_id`/`installation_id` — self-healing via GitHub's
own clean retry, so `warning` not `error`, but still a signal, not silence.
Rollback: revert
the migration (`alembic downgrade -1`) and the route change together — the
new columns/table have no other reader yet, so there's no partial-rollback
hazard.

**Acceptance:** the Integration tests above pass in CI (`pytest -m
integration`, already gated on every PR per Phase 0's `ci.yml`); `alembic
upgrade head` then `alembic downgrade base` both succeed against a fresh
`testcontainers` Postgres, matching Phase 0's own migration-reversibility
bar; `mypy --strict` confirms `tenancy/resolution.py` imports nothing from
`adapter/storage` **or `adapter/github`** directly (only the `TenancyStore`
Protocol and its own domain-owned `WebhookEvent` model — the second import
this check now covers, corrected by this pass, since an earlier draft's
`resolve_installation` signature took `GitHubWebhookEnvelope` directly and
this check would have missed that violation entirely), matching
the same Protocol-boundary proof every other phase's Acceptance section
requires for its own domain/adapter pair; `docs/adr/0005-pure-no-io-logic-unit-tested.md`
exists and is merged as part of this phase's own PR, not left as a forward
promise to Phase 2 — the same "ADR merged before/with the phase's PR" bar
Phase 2's own Acceptance section holds itself to for that ADR's later
extension.

## Phase 2 — Pipeline step framework

**Why here:** the hard prerequisite for Phases 4–7 (Units D–G) — none of
the concrete steps can be built against a framework that doesn't exist yet.
Independent of Phase 1 (no shared code) and of Phase 3 (Worker dispatch),
which is sequenced after this phase only because dispatching *to* something
is more meaningfully testable once that something is real, not because of a
hard code dependency either way.

- New modules in `review/` (the domain package `architecture.md` and
  `review/__init__.py`'s own docstring already designate for exactly this:
  "orchestrates the review pipeline"):
  - `review/step.py` — the `Step` `typing.Protocol` (1–3 methods per
    `architecture.md`'s Protocol-size rule: `name`, `run(input, context) ->
    output`; `input`/`output` are step-specific pydantic models,
    `context` is one shared `StepContext` pydantic model — `job_id` and a
    `dry_run` flag — identical across every step, resolving the dry-run
    decision below without special-casing any one step's signature; a
    declared `depends_on: tuple[str, ...]` of upstream step names for
    dependency-graph resolution).
  - `review/pipeline.py` — the registry/runner: resolves execution order
    from declared dependencies, runs steps whose dependencies are already
    satisfied concurrently via `asyncio.gather`, catches and isolates
    sub-step failures, propagates macro-step failures as a fatal
    `PipelineFailedError`. **Per-step timeout, specified here rather than
    left as a claim other phases cite without a source — corrected in this
    pass, since an earlier draft referenced "the per-step timeout" from
    Phase 3's stuck-job reasoning, Phase 4's retry-budget note, and the
    Security review's DoS-mitigation framing without this module ever
    actually defining it:** the registry wraps every step's `run(...)`
    call in `asyncio.wait_for(..., timeout=<that step's own configured
    timeout>)`; a timeout is handled identically to any other exception the
    step raises — isolated per US-F5 for an analyzer sub-step (Phase 5's
    inner registry) and fatal per US-F6 for a macro step, using the same
    isolation/fatality logic already described above, not a second
    parallel failure path. **Per-step, not one shared value — this now
    matches `pipeline-step-framework.md`'s NFR text directly
    ("Each step declares its own timeout with a default of 30s ...
    overridable per step via the same bootstrap-config mechanism as its
    on/off flag"), corrected from an earlier draft of this bullet that
    built one shared, non-overridable value instead and defended it as
    avoiding speculative generality — that defense doesn't survive contact
    with the PRD text, which asks for per-step configurability explicitly,
    and doing so costs nothing architecturally: it mirrors the exact
    per-step-field shape `StepEnablement` (below) already establishes for
    the on/off flag, not a new config pattern.** `asyncio.wait_for`
    (stdlib) is the correct tool here and needs no new dependency: every
    step's `run()` awaits ordinary async I/O under the hood (an LLM
    completion via `httpx`, a GitHub call via `httpx`, DB I/O via
    `asyncpg`, or — for the two analyzer sub-steps — a subprocess awaited
    via `asyncio.create_subprocess_exec`), and `asyncio.wait_for` cancels
    any of those cleanly by cancelling the awaiting coroutine.
  - `review/config.py` — a plain, non-settings `StepEnablement` pydantic
    model (one boolean field per registered step) that `review/pipeline.py`
    accepts via constructor injection, plus a new sibling `StepTimeouts`
    pydantic model (one integer-seconds field per registered step,
    mirroring `StepEnablement`'s shape **with one deliberate exception,
    not an unreconciled contradiction**: the two analyzer sub-steps get
    independent enablement booleans, per US-F4, since Gitleaks and Semgrep
    genuinely need to be toggled separately, but share one timeout field
    below rather than two, since neither has a documented reason to need a
    different time budget from the other — enablement and timeout are
    configured at the granularity each one actually needs, not forced into
    the same shape for its own sake) that `review/pipeline.py`
    also accepts via constructor injection. The bootstrap fields themselves
    live only in `WorkerSettings` (`apps/worker/settings.py`, extended with
    one boolean per registered step as before, plus one
    `..._step_timeout_seconds: int` field per macro step and one shared
    `static_analysis_analyzer_timeout_seconds: int` field covering both
    analyzer sub-steps — a single shared sub-step default, not two
    separate per-analyzer fields, since neither analyzer has a documented
    reason to need a different budget from the other; only the
    static-analysis *macro* step's own budget has a documented reason to
    differ from its siblings, per the bullet below):
    - `prefilter_step_timeout_seconds: int = 30` — matches the PRD's
      stated 30s default (mirroring `WorkerSettings.llm_timeout_seconds`,
      unchanged from Phase 0); this step's own I/O is two bounded GitHub
      calls (Phase 4's Files-endpoint pagination and one Trees-API call).
    - `static_analysis_step_timeout_seconds: int = 240` — the one step
      that genuinely needs a larger budget than the PRD's 30s default, and
      named as such rather than silently deviating: this macro step's
      `run()` (Phase 5) covers, in sequence, (1) up to ~50 sequential
      batches of 10-way-concurrent Blob-content fetches (the 500-file
      materialization ceiling ÷ the 10-way bounded concurrency), (2) both
      analyzer sub-steps running concurrently, each individually bounded
      by `static_analysis_analyzer_timeout_seconds` below (worst healthy
      case ≈ that sub-step budget, not summed, since they run via
      `asyncio.gather`), and (3) up to 50 sequential redaction-pass
      Gitleaks subprocess spawns (Phase 5's finding-count ceiling). `240`
      gives comfortable headroom over a healthy-case estimate of these
      three phases combined (content-fetch tens of seconds; analyzer phase
      up to the sub-step budget below; redaction pass well under a
      minute for 50 short-lived invocations) — see the dedicated headroom
      note in Phase 5 for why this margin also has to cover the isolation
      guarantee (US-F5), not just the healthy-case sum.
    - `static_analysis_analyzer_timeout_seconds: int = 60` — the
      sub-step-level timeout for each individual analyzer invocation
      (Gitleaks or Semgrep), nested well inside the outer
      `static_analysis_step_timeout_seconds` budget above.
    - `llm_review_step_timeout_seconds: int = 90` — covers the LLM
      completion call itself (already separately bounded by the existing
      `WorkerSettings.llm_timeout_seconds = 30`, Phase 0, unchanged) plus
      this step's own reuse of the redaction pass, capped at 20 sequential
      Gitleaks subprocess spawns (Phase 6's finding-count ceiling) — a
      smaller version of the same static-analysis-step tradeoff, with a
      smaller cap and so a smaller step budget. **Headroom shown, not just
      asserted, correcting an earlier draft that left this budget's fit
      unverified while static-analysis's own budget showed its sum:** using
      the same per-call redaction-latency estimate static-analysis's own
      budget implies (up to 50 short-lived Gitleaks invocations "well under
      a minute," i.e. well under ~1.2s/call), 20 redaction calls cost at
      most ≈24s; combined with the 30s LLM-call bound, worst-case healthy
      work is ≈54s, leaving `90 − 54 = 36` seconds (~40%) of headroom — a
      materially thinner margin than static-analysis's own (that step's
      work fits in well under half its 240s budget), but no longer the
      bare ~10%/6s an earlier draft's unexamined `60` default would have
      left, which risked a false-positive fatal timeout on an otherwise
      healthy job doing legitimately busy redaction work.
    - `output_step_timeout_seconds: int = 30` — this step's own I/O is
      GitHub posting calls (Phase 7), retry-scoped to only the safe-to-retry
      cases (Phase 4's `adapter/github/http.py` bullet), so it stays at the
      PRD's 30s default like pre-filter.
    `apps/worker/main.py`,
    the composition root, reads `WorkerSettings`
    once and constructs the `StepEnablement` and `StepTimeouts` instances
    passed into the registry.
    `review/config.py` itself has no `pydantic-settings` import and reads
    no env vars — the domain package's zero-infra-imports rule holds
    exactly as it does for every other module in `review/`.
- The registry is used at **two nested levels** per the framework PRD: one
  instance for the four macro pipeline steps (pre-filter, static-analysis,
  llm-review, findings-merge-and-output — Phases 4/5/6/7 below each
  register one), and the static-analysis step (Phase 5) instantiates a
  second registry internally for its analyzer sub-steps.
- Startup validation (`apps/worker/main.py`, extending the existing
  fail-fast pattern already there for the LLM-reachability check): refuses
  to start if the enabled-step configuration can never produce a review
  (e.g. every analyzer disabled and llm-review disabled) — mirrors
  `llmgateway/startup_check.py` and `adapter/storage/pgvector_check.py`'s
  existing shape exactly (a pure check function + the composition root
  deciding to `SystemExit(1)`).
- **ADR extended here, created in Phase 1 — flagged, not silently
  deviated, and scoped broader than one earlier draft of this bullet
  covered:** `testing.md`'s "every user story maps to at least one
  integration test, not a unit test" rule is stated without a fakes/no-I/O
  carve-out, and states any deviation "requires a new ADR, not a one-off
  exception." `pipeline-step-framework.md`'s own Testing table nonetheless
  specifies unit-only coverage for US-F1/F2/F3/F4/F6/F7, reasoning that
  they're pure orchestration logic with no adapter or external system in
  the loop — which is true, and also means no test at those stories'
  granularity can structurally satisfy testing.md's own middle-tier
  definition ("one real adapter... against a real or high-fidelity
  recorded external system"). The same structural gap recurs twice more
  elsewhere in this plan, not only at the story-framework level: US3
  (Phase 4's pre-filter, Unit-only per the PRD's own Testing table — pure
  filtering logic, no I/O) and, at the sub-component level, pure no-I/O
  helpers that happen to be *implemented inside* an adapter package
  testing.md otherwise assigns wholesale to the Integration tier
  (`adapter/github/triggers.py`'s lookup table, Phase 1;
  `adapter/analyzers/subprocess_runner.py`'s path-traversal/mode-guard
  logic, Phase 4/5) — a pure function with no I/O of its own doesn't
  structurally fit an Integration-tier definition just because the file
  it lives in also contains I/O elsewhere. Rather than let the plan's
  Tests subsections silently diverge from testing.md in three separate,
  unreconciled places, this plan resolves all three cases under one
  principle in one ADR — pure, no-I/O logic gets Unit-tier coverage
  regardless of the story it serves or the package it's physically
  implemented in — rather than three unreconciled one-offs.
  **Sequencing correction:** `docs/adr/0005-pure-no-io-logic-unit-tested.md`
  is created in Phase 1's own PR, not this one, since Phase 1's
  `triggers.py` is the first case needing the carve-out and Phase 1 merges
  before this phase does — see Phase 1's own ADR bullet. This phase's PR
  extends that already-merged ADR's scope to cover its own remaining two
  cases (US3's pre-filter coverage, `subprocess_runner.py`'s path-guard
  logic, both landing in Phase 4/5) rather than creating a second ADR or
  re-creating the first. Phase 7's new
  `review/test_review_job.py` (see Phase 7) still exercises the real,
  fully-wired registry under integration conditions and incidentally
  covers US-F3/US-F4/US-F6's behavior against real steps, which this ADR
  cites as the closest the framework's own stories get to boundary-crossing
  coverage, without claiming it substitutes for a dedicated integration
  test per story.

**Tests:** Unit (per `testing.md`'s tier rule and the ADR above — this is
pure orchestration logic over fakes at the `Step` Protocol boundary, no
real I/O, adapters, network, or database), matching
`pipeline-step-framework.md`'s own Testing table exactly:
`review/test_step_boundaries.py` (US-F1, import-boundary check),
`review/test_step_registry.py` (US-F1 downstream-tests-unmodified case,
US-F2 concurrency-via-wall-clock-timing, US-F3 macro-step registration,
US-F4 disabled-step-never-invoked, US-F6 fatal macro-step short-circuits
downstream, plus a dedicated case asserting a fake macro step that sleeps
past a deliberately short test-configured `StepTimeouts` value for that
step is treated exactly like a raising macro step — fatal,
short-circuiting downstream, the same US-F6 Given/When/Then's "exceeds its
timeout" clause the raising case above proves the "unhandled exception"
clause of — proving the new `asyncio.wait_for` mechanism rather than
leaving it as an unverified claim, plus a companion case proving
`StepTimeouts` is genuinely per-step, not one shared value in disguise: a
`StepTimeouts` instance configured with two different values for two
different fake steps in the same test run, asserting the short-timeout
step's `run()` is cancelled while the long-timeout step's identical-length
sleep completes normally — the specific property this round's rebuild
from one shared, non-overridable value introduced and that the
single-step timeout case above doesn't, by itself, prove), `review/test_static_analysis_step.py`
(US-F3 sub-step
registration, US-F4 disabled-sub-step, US-F5 isolated sub-step failure,
plus the equivalent timeout-vs-isolation case at the sub-step level —
this file is created in this phase against a generic fake sub-step
registered purely for test purposes, since `review/static_analysis.py`
itself doesn't exist until Phase 5; Phase 5 extends this same file with
its own additional cases once the real module lands, rather than
duplicating it),
and `apps/worker/test_settings.py` extended for US-F7 (all-disabled
startup validation, plus a companion case with one analyzer enabled and
llm-review disabled asserting startup succeeds — the negative branch of
the same "can this configuration ever produce a review" predicate).
US-F3/US-F4/US-F6 additionally get real-registry
coverage as a byproduct of Phase 7's `review/test_review_job.py`
integration test, per the ADR above. **Marker granularity, flagged now
since it becomes load-bearing once Phase 5 extends this file:**
`review/test_static_analysis_step.py` is Unit-tier only in this phase (a
fake sub-step, no real binaries), so the whole file can carry a
module-level `pytestmark` here — but once Phase 5 adds real-binary
Integration-tier cases to the same file (see that phase's own Tests
note), the file straddles both tiers and `@pytest.mark.integration` has to
move to per-test application for the Integration-tier cases specifically,
matching `testing.md`'s CI-gating split (`pytest` vs `pytest -m
integration`) which a single module-level marker can't express once a file
mixes tiers.

**Observability & rollback:** Every step logs start/completion/failure with
`job_id`, `step_name`, `duration_ms`, and `status` (including steps skipped
because disabled) — the structured-field convention `apps/worker/logging_config.py`
already establishes, extended with these fields rather than a new logging
setup. **Default log level for a macro step's failure, stated once here
rather than left for later phases to each decide independently:** a macro
step's failure that results in the job's terminal `FAILED` status logs at
`error` by default — this is what makes `PipelineFailedError` propagate to
Phase 3's dispatch loop in the first place, so by construction it's always
a confirmed defect for that job, not a routine, self-healing condition.
Later phases' own specific failure modes override this default only where
they've named a more specific convention (e.g. Phase 4's GitHub
retry-exhaustion `warning`, which is absorbed by a retry that doesn't fail
the job outright) — every other fatal macro-step case (a malformed LLM
response, Phase 6; a redaction call that times out, Phase 5/6) falls back
to this `error` default rather than inheriting whatever level the step's
own internal logging happened to use. This default does not apply to the
separate, explicitly non-fatal finding-count ceilings (Phase 5's 50-cap,
Phase 6's 20-cap) — those are capped-and-logged at `warning`, not a step
failure at all. No running-service rollback concept yet: nothing outside this
package consumes the framework until Phase 3 wires the Worker's dispatch
loop to it.

**Acceptance:** all Unit tests above pass; `docs/adr/0005-pure-no-io-logic-unit-tested.md`
(created in Phase 1, per that phase's own ADR bullet) is extended to cover
this phase's two additional cases before this phase's PR merges, not left
as a forward promise; `mypy
--strict` confirms `review/` has zero references to any concrete step's
implementation (no import of anything from `adapter/analyzers`,
`adapter/llm`, or `adapter/github` inside `review/step.py`/`review/pipeline.py`)
— the mechanical proof that the framework genuinely doesn't know which
steps exist, per US-F1's boundary check.

## Phase 3 — Worker job dispatch loop

**Why here:** the PRD's Unit B has no hard code dependency on Phase 2, but
sequencing it after the framework exists means its own tests exercise a
real (if still step-less) registry rather than a throwaway fake, and its
"what does claiming a job actually *do*" question has a real answer by the
time this phase is written.

- Replaces `apps/worker/main.py`'s current terminal behavior (`logger.info("worker
  idle: no job dispatch wired yet")`, then `main()` returns and the process
  exits 0 — see its own docstring, "Replaced by a real claim/dispatch loop
  once a later phase's job-queue dispatch logic exists") with an actual
  loop: after the existing LLM-reachability startup check and Phase 2's
  step-configuration validation both pass, poll
  `JobQueueRepository.claim_next()` (already implemented,
  `adapter/storage/queue.py`, unchanged) every 2 seconds when the queue is
  empty — a concrete default, not left unspecified, via a new
  `WorkerSettings.poll_interval_seconds` field matching every other numeric
  bootstrap-config field's existing pattern — and for each claimed job,
  dispatch by `job_type` — `REVIEW` jobs go to Phase 2's
  pipeline registry; `INDEX` jobs (the C4 doc's other job type) are logged
  and left unimplemented, since indexing is Phase 2 of the *roadmap*, not
  this plan. **Terminal status, stated rather than left implicit:** an
  `INDEX` job is marked `DONE` immediately after being logged — "logged
  and left unimplemented" is itself this job's complete, correct handling
  for Phase 1's scope (there's no indexing logic to run, so nothing is
  pending), not a partial/deferred state. Leaving it `CLAIMED` would be a
  deterministic, guaranteed-to-recur gap for every `INDEX` job the queue
  ever receives, unlike the genuinely rare failure modes below that stay
  `CLAIMED` pending Phase 8's reclaim-timeout work.
- Job status transitions: `CLAIMED` → `DONE` on pipeline success, `CLAIMED`
  → `FAILED` on a fatal macro-step failure (per Phase 2's
  `PipelineFailedError`) — a small addition to `adapter/storage/queue.py`
  (a `mark_done`/`mark_failed` method; `claim_next` already exists and is
  unchanged).
- **Composition-root wiring for storage, closing the same gap on the
  Worker side that Phase 1's own equivalent bullet closes for the Webhook
  API:** `WorkerSettings` (`apps/worker/settings.py`) gains the same
  `database_url: SecretStr` field, sourced the same way (via
  `pydantic.Field(validation_alias="REVIEW_DATABASE_URL")`) — see Phase
  1's bullet for the full reasoning, not repeated here.
  `docker-compose.yml`'s `worker` service block also needs
  `REVIEW_DATABASE_URL` added to its own `environment:` section.
  `apps/worker/main.py`'s `main()` calls `create_storage_engine`/
  `create_session_factory` (`adapter/storage/engine.py`, unchanged) once at
  startup, alongside the existing LLM-reachability check, and constructs
  `JobQueueRepository(session_factory)` — the concrete instance the
  dispatch loop below actually calls `claim_next()`/`mark_done()`/
  `mark_failed()` against, closing the gap where this phase's text
  otherwise assumed a repository instance existed without ever building
  one.
- **Compatibility break, handled here:** `docker-compose.yml`'s `worker`
  service currently has `restart: "no"` and no healthcheck, with an
  explanatory comment that a clean `exit 0` *is* success at Phase 0 (no
  dispatch loop exists yet). Once this phase makes the Worker loop forever,
  that comment and the exit-0-means-healthy assumption are both stale. This
  phase updates `docker-compose.yml`'s `worker` service to `restart:
  unless-stopped` and rewrites `tests/e2e/test_compose_stack.py`'s
  `test_worker_boots_cleanly_without_crash_looping` (which currently
  asserts `exit_code == 0` and `restart_count == 0` — an assertion this
  phase makes permanently false) into an assertion that the container stays
  in the `running` state and logs "worker started" without restarting
  within the test's polling window. See Compatibility & rollout.

**Tests:** Integration (in `apps/worker/test_dispatch.py`, real Postgres,
real claim/dispatch against a directly-enqueued test job — no HTTP/webhook
layer involved, matching the PRD's own "testable with directly-enqueued
test jobs independent of Unit A" note): a claimed job with a
registered-but-trivial pipeline (Phase 2's registry with a fake single
step) transitions `PENDING` → `CLAIMED` → `DONE`; a claimed job whose
pipeline raises transitions to `FAILED`; a case that forces an unhandled
exception in the dispatch loop itself (not the pipeline it calls — e.g. a
fake pipeline runner monkeypatched to raise outside the normal
success/`PipelineFailedError` path) asserts the already-`CLAIMED` job is
still marked `FAILED`, the `error` log fires, and the loop survives to
process a subsequent job, proving the bug-resilience behavior described
below rather than leaving it as an unverified claim in the Observability
section — matching the equivalent `error`-level assertion the sibling
`claim_next()`-failure case below already gets; a case with a fake
pipeline step that sleeps past a deliberately short (test-configured)
`stuck_job_warning_threshold_seconds` asserts the `warning` log fires at
least once, naming the `job_id`, before the job still completes normally
(transitions to `DONE`) once its still-running pipeline finishes —
proving the mechanism surfaces the signal without altering the job's own
outcome. **The combined case — a job that crosses the warning threshold
and then raises — is deliberately not a separate test:** the two
mechanisms it would exercise are already proven independently above (the
raising case proves `task.result()`'s exception propagation; the
stuck-then-completes case proves the warning fires without altering
outcome), and both share the same `await asyncio.wait(...)` loop with no
state that differs between "warned, then succeeds" and "warned, then
raises" — the warning log and the eventual `task.result()` call are
sequential, independent steps in the same loop iteration, so a third test
combining them would exercise the same code paths the first two already
cover individually, not a new one. A further case, proving the
`claim_next()`-itself-failing behavior below rather than leaving it as an
unverified claim (the same "prove it, don't just assert it" bar the
dispatch-loop-exception case above already meets): a fake/monkeypatched
`claim_next()` that raises on its first call asserts the `error` log fires
with no job to mark `FAILED` (none was ever claimed), and that the loop
survives to poll again and successfully claim, dispatch, and complete a
real subsequent job on a later `claim_next()` call that succeeds — proving
recovery, not merely non-crashing. A case that claims and dispatches an
`INDEX`-type job (the dispatch-by-`job_type` branch this phase's loop also
implements, alongside `REVIEW`) asserts it's logged as unimplemented,
transitions to `DONE` (its complete, correct terminal status for this
phase's scope, not left `CLAIMED`), and the loop survives to process a
subsequent `REVIEW` job normally, rather
than crashing or silently dropping the `INDEX` job — the one dispatch
branch besides `REVIEW`'s otherwise going untested.

**Observability & rollback:** Claim/dispatch/completion each log with
`job_id`, `job_type`, and outcome — at `info` for a claim, a dispatch, or a
`DONE` completion, and at `error` for a `FAILED` completion, per Phase 2's
own stated default for a macro step's fatal failure (this loop is simply
where that `error`-level completion log actually gets emitted for every
`REVIEW` job, regardless of which macro step or failure cause produced the
`PipelineFailedError`). An unhandled exception escaping the
dispatch loop itself (not a pipeline-reported failure, but a bug in the
loop) logs at `error`, the already-`CLAIMED` job for that iteration is
marked `FAILED` before the loop continues to the next poll (so it doesn't
stay permanently `CLAIMED` with no recovery path), and the process itself
does not crash — a crash-looping Worker pod is worse than one bad job
marked `FAILED` for investigation. **Residual gap, acknowledged rather than
silently assumed away, the same treatment Phase 7 gives its own
structurally identical case:** this crash-resilience design still assumes
the `mark_failed()`/`mark_done()` write itself succeeds. If that write
also fails (e.g. a Postgres blip coinciding with the very failure being
handled), the job stays permanently `CLAIMED` with no distinct log line
marking the double failure — the same reclaim-timeout gap named below
would eventually need to cover this case too; not solved here, just named
rather than left implicit. **`claim_next()` itself failing (e.g.
Postgres unreachable/restarting), not only post-claim job processing,
addressed explicitly — a gap this plan's own review caught, since the
crash-resilience description above only ever covered exceptions raised
*after* a job is claimed, and the document's global "Compatibility &
rollout" section separately analyzes the equivalent Postgres-outage case for the
webhook API's write path without an equivalent analysis here, despite this
phase making the same kind of Postgres dependency newly load-bearing for
the Worker:** a `claim_next()` call that itself raises (rather than
returning "no job available") is caught at the same poll-loop level as a
post-claim exception, logged at `error`, and the loop continues polling at
its existing interval rather than propagating and crashing the process —
without this, a transient Postgres blip during the poll (before any job is
even claimed) would crash-loop the container under this phase's own
`restart: unless-stopped` change (see that bullet below), which is exactly
the failure mode the post-claim handling above exists to prevent, just one
step earlier in the same loop. **Known, temporary gap at this phase's
own point in the plan, closed by Phase 7, not solved here:** a `PENDING`
→ `CLAIMED` → `FAILED` transition at this phase produces no GitHub-visible
signal — no phase before Phase 7 has any GitHub-posting capability at all,
so there's nothing to post yet. That's an acceptable, real gap for Phase
3's own scope (no earlier phase could close it), but it means a fatal
pipeline failure is invisible outside a Postgres query until Phase 7 lands
and extends this same dispatch loop to also post a failure-conclusion
check-run on any fatal failure — see Phase 7's `review/output.py` bullet
for the full fix and why it has to cover every macro step, not only
llm-review's. **Correction — reclaim is NOT
implemented in this phase, and rollback does not rely on it:** `claim_next()`
(`adapter/storage/queue.py`) only ever selects `PENDING` rows; it never
re-examines stale `CLAIMED` rows, so a job whose Worker process is
terminated mid-processing (a crash, or a redeploy that kills the container
before it reaches a terminal status) stays `CLAIMED` forever with **no**
automatic recovery in this phase — no reclaim-timeout logic exists yet
(flagged as a real Phase 8 hardening gap, not silently assumed solved).
**Cheap, in-scope mitigation for the gap's visibility, not its
resolution — simplified from an earlier draft's separately-scheduled
watchdog `asyncio.Task`, which this plan's own review caught introducing
exactly the kind of independent failure-handling surface (a task whose own
exceptions aren't covered by the dispatch loop's existing crash-resilience
wrapping described above) this phase otherwise avoids, and which the scope
review judged more elaborate than the gap it addresses actually calls
for:** the loop still claims and processes one job at a time to completion
before claiming the next (unchanged from this phase's core design above),
but instead of `await`ing the job's pipeline-execution coroutine directly,
it wraps that same coroutine in a single `asyncio.Task` and awaits it in a
loop — `await asyncio.wait({task}, timeout=stuck_job_warning_threshold_seconds)`
— logging at `warning` (not `error`: this is a "plausibly stuck, worth an
operator's attention" signal, not a confirmed platform defect the way an
unhandled dispatch-loop exception is, so `error` stays reserved for that
stronger case) with the job's `job_id` each time the wait times out, before
looping back to wait again, until the task appears in `asyncio.wait`'s
returned `done` set. **Exception propagation, specified rather than left
implicit — `asyncio.wait()` alone does not do this:** once the task is in
`done`, the loop explicitly calls `task.result()` (re-raising whatever
exception the pipeline coroutine raised, if any) rather than treating the
task's mere presence in `done` as success — `asyncio.wait()` returns which
tasks finished, it does not re-raise their exceptions on its caller's
behalf, so skipping this step would silently swallow exactly the kind of
in-loop bug this mechanism exists to surface. There is no
separate task lifecycle to manage or fail independently of the loop — the
`asyncio.wait` call plus the explicit `task.result()` *is* the main loop
for that job, so it's covered by the
same try/except this phase's own bug-resilience behavior already wraps
around job processing (above), and a bug in the warning-logging path
itself surfaces exactly the same way any other in-loop bug would, not as a
silently-swallowed task exception. **Threshold basis, named rather than
left as an unsourced "expected duration":** the threshold is a flat configured value,
`WorkerSettings.stuck_job_warning_threshold_seconds` (default `450`).
The four macro steps run strictly sequentially (pre-filter depends on
nothing; static-analysis depends on pre-filter's output; llm-review
depends on static-analysis's findings; findings-merge-and-output depends
on llm-review's output — see each phase's own "Why here" note), so the
worst-case healthy-run duration is their sum, not their max: `30`
(pre-filter) + `240` (static-analysis) + `90` (llm-review, Phase 2's
configured step timeout) + `30` (output) = `390` seconds. `450` gives
this sum roughly 15% headroom, comfortably longer than the worst
healthy-run total without being so large that a genuinely stuck job goes
unwarned for an unreasonable stretch — so it doesn't fire during ordinary
slow-but-healthy processing, while still surfacing a job that's clearly
exceeded every timeout this pipeline otherwise enforces on itself), not a
dynamically computed "expected pipeline duration" this plan has no data
source for.
This is a Worker-process-internal check against data the loop already
holds — an `asyncio.Task` wrapping the same coroutine the loop would have
awaited directly, plus stdlib `asyncio.wait` in a loop, needing no new
infra or third-party dependency — distinct from real liveness probing
(still correctly deferred to Phase 8 below). This turns "an operator has
to think to run a SQL query" into "the existing structured-log tooling
this phase already relies on surfaces it," without solving the underlying
reclaim gap itself.
Rollback: redeploy the prior Worker image; any job left `CLAIMED` by the
terminated old-version process requires manual intervention (an operator
resetting its status to `PENDING` directly) to be reprocessed — this plan
does not claim otherwise. **Liveness gap, also flagged rather than silently
unaddressed:** an idle Worker (empty queue) produces no log output between
polls, so a genuinely hung process (stuck past the point where its own
exception handling would catch and log) is not distinguishable from a
healthy-but-idle one by logs alone, and this phase adds no Compose/Helm
liveness probe for the Worker (it has no HTTP surface to probe). Acceptable
for this phase's own scope — Docker/Kubernetes still detects a hard crash
regardless — but real liveness probing (e.g. a periodic heartbeat file or a
minimal local health endpoint) is Phase 8 (production hardening) scope, not
solved here.

**Acceptance:** the Integration tests above pass; the rewritten E2E test
(`tests/e2e/test_compose_stack.py`) asserts the `worker` service stays
`running` (not `exited`) for the test's observation window and never
restarts.

## Phase 4 — Diff fetch + Pre-filter step

**Why here:** the simplest of the four concrete steps, first among Units
D–G to prove the framework from Phase 2 actually plugs in a real step with
zero changes to `review/step.py`/`review/pipeline.py` — a fast, low-risk
proof point before the higher-risk analyzer/LLM/output phases. **Correction
surfaced during exploration (sequencing gap, not an assumption):** Phase
1's enqueue payload carries only PR-identifying fields (org/repo id, PR
number, head SHA, installation id) — no changed-file list and no file
content. Pre-filter needs the former; Phase 5's analyzers need the latter
on disk. The original draft deferred all diff-fetching to Phase 6 (for the
LLM's bare-diff input only), which left Phase 4/5 with nothing to consume.
This phase therefore pulls `ingestion/diff.py` + `adapter/github/diff.py`
forward from where an earlier draft placed them (alongside Phase 7's
posting client), plus one more module: **`adapter/github/auth.py` itself —
corrected here from an earlier, self-contradictory draft that had this
phase call an installation-token provider "built in Phase 7" while
depending on it to function and be tested.** `diff.py` cannot fetch
anything without a real installation token, and this phase's own
Integration tests need a real (cassette-recorded) token exchange to run —
so `auth.py` is built and tested in this phase's own PR, not merely
consumed ahead of its "real" landing point. Phase 7 (posting) reuses the
same client unchanged; see Phase 7's own note for why it needs no auth.py
work of its own. All three pieces are small and self-contained enough to
build once here and reuse through Phase 6/7, which consume the same `Diff`
and the same token-provider client this phase produces rather than each
fetching or authenticating on their own.

- New `ingestion/diff.py` + `adapter/github/diff.py`: the first code
  fetching a PR's changed files/patches via the GitHub API, using
  `adapter/github/auth.py`'s installation token. **Placement:**
  `ingestion/diff.py` isn't itself one of `DiffFetcher`'s consumers
  (`review/prefilter.py`, `review/static_analysis.py`, and
  `review/llm_review.py` are, per below); it's the package that owns the
  `Diff` model the Protocol operates over and hands to every consumer —
  `architecture.md`'s Protocol-boundary rule is satisfied by defining the
  Protocol alongside the data model it produces, not only by defining it
  at a consumer, and `ingestion/` is this plan's designated home for
  exactly that kind of shared input model (mirroring, at a smaller scale,
  how `findings/models.py` owns `Finding` for every consumer that produces
  or reads one, and how `findings/redact.py` owns `TextRedactor` for the
  same reason — Phase 5's own bullet — rather than either Protocol being
  defined inside whichever step happens to be its first consumer).
  `ingestion/diff.py`
  defines a `DiffFetcher` `typing.Protocol` with two methods:
  `fetch(...) -> Diff` (`Diff` a pydantic model listing, per changed file:
  path, unified-diff `patch` text, real git `mode` (`100644`/`100755`/
  `120000`/`160000`), a nullable `blob_sha` — `None` for a `removed`
  file, the blob GitHub holds for anything still present at the head
  commit — and a nullable `blob_size_bytes: int | None` (`None` alongside
  `blob_sha` for a `removed` file), sourced from the same Git Trees API
  response's own per-entry `size` field described below, so pre-filter's
  aggregate-bytes ceiling (below) has a byte figure to check against
  without needing to fetch content first), and `fetch_content(paths) -> dict[path, bytes]` (Phase 5's
  static-analysis step calls this after pre-filter narrows the path set —
  see the file-materialization bullet below for why content-fetching is
  a second, later call rather than part of `fetch()` itself). Consumed by
  `review/prefilter.py` (this phase, `fetch()` only),
  `review/static_analysis.py` (Phase 5, `fetch_content()` only), and
  `review/llm_review.py` (Phase 6, `fetch()`'s patch data only — Phase 6
  now depends on this phase for the diff data instead of fetching it
  itself); `adapter/github/diff.py` implements it, matching the
  `StaticAnalyzer`/`LLMClient` boundary pattern used elsewhere in this
  plan — the concrete implementation is
  wired in only at the composition root. No AST parsing, no index — bare
  diff only, per the roadmap.
- **ADR gap, caught by this plan's own review — the placement bullet above
  states a rule without the process `architecture.md`'s own header demands
  for stating it:** `architecture.md` says "Deviation requires a new ADR,
  not a one-off exception," in the same unqualified terms Phase 1's
  `triggers.py` unit-tier carve-out took seriously enough to spin up
  `docs/adr/0005-pure-no-io-logic-unit-tested.md` for. `DiffFetcher`'s
  defined-at-the-data-model placement above is exactly that kind of
  deviation from "defined at the consumer" and had, until this correction,
  only plan-prose justification — no ADR — which is inconsistent with how
  this same plan treated the testing-tier deviation one phase earlier. This
  phase's own PR therefore creates `docs/adr/0006-protocol-defined-at-data-model.md`,
  documenting the general rule the placement bullet above already argues
  for in prose, covering two shapes of the same underlying deviation: a
  Protocol may be defined alongside the pydantic data model it **produces**
  (`DiffFetcher`/`Diff`, the concrete case here), or, where a Protocol
  produces no data model of its own but substantively **operates on** one
  bounded context's data (Phase 5's `TextRedactor`, which mutates
  `findings/models.py`'s `Finding` text fields and returns `str`), it lives
  in that data's own package instead — either way, rather than strictly at
  the first real consumer, when more than one consumer would otherwise each
  need to import it from an arbitrarily-chosen "first" consumer module.
  This ADR is scoped to Protocol-placement
  deviations specifically; it does not cover Phase 7's separate
  plain-function-vs-Protocol acceptance for `findings/markdown.py` (see that
  phase's own bullet), which is a different kind of deviation — no Protocol
  exists there at all, by design, not a Protocol placed somewhere other than
  the first consumer — and stands on its own reasoning, not this ADR's.
- **Concrete source for `mode`/content, revised from an earlier, flawed
  draft of this bullet that this plan's own review caught before it
  shipped — two GitHub calls, not a per-file Contents-API call:** (1)
  `GET /repos/{owner}/{repo}/pulls/{pull_number}/files`, **followed across
  every page via its `Link` header** (GitHub paginates this endpoint; a
  single-page call would silently lose coverage of any PR touching more
  files than one page) — gives the changed-file list, `status`
  (`added`/`modified`/`removed`/etc.), and unified-diff `patch` text (used
  for the LLM's bare-diff prompt and pre-filter's path-based filtering);
  (2) **one** `GET /repos/{owner}/{repo}/git/trees/{head_sha}?recursive=1`
  call — the Git Trees API, which returns every path in the tree with its
  real git `mode`, blob `sha`, and blob `size` (bytes) in a single
  response, cross-referenced by path against (1)'s file list. This is the
  actual octal git mode Phase 5's symlink/submodule guard checks — not a
  derived value from a differently-shaped field, avoiding the
  type-to-mode mapping ambiguity an earlier draft (using the Contents
  API's `type` field) would have left unspecified, and avoiding a per-file
  API call just to learn the mode. The same response's `size` field is
  what populates `Diff.blob_size_bytes` above — one API call already made
  for the mode/sha data also supplies the byte figure pre-filter's
  aggregate-bytes ceiling needs, with no second call. Full file
  **content** (as opposed to mode/size) is deliberately NOT fetched
  here — see the next bullet for why, and where.
- **New dependency, moved here from a later phase — same sequencing
  correction:** RS256 JWT signing (both `adapter/github/auth.py`, built in
  this phase — see the dedicated bullet below — and this phase's own
  startup-time private-key validation below, which calls `jwt.encode()`
  directly) has no coverage in this repo's
  existing dependencies or the stdlib — `pyjwt[crypto]` (pulls in
  `cryptography` as its RS256 backend) is added to `pyproject.toml`'s main
  dependencies starting here, since this phase is the first real caller.
  Checked against the reuse order first: no existing helper in this
  codebase signs a JWT (`manifest.py`'s HMAC-based state signing is a
  different primitive — symmetric HMAC, not asymmetric RS256 — and doesn't
  cover this), stdlib has no JWT/RS256 support, no other already-installed
  dependency (FastAPI, SQLAlchemy, LiteLLM) bundles one either.
  **Second, pre-existing gap this same dependency check surfaced, not
  introduced by this plan but fixed here since this phase is the first to
  make GitHub API calls load-bearing for a real, always-on code path:**
  `httpx` is already imported and called by `adapter/github/manifest.py`'s
  `exchange_temporary_code` (Phase 0), but `pyproject.toml` lists it only
  under `[dependency-groups].dev`, not `[project].dependencies` —
  confirmed against the actual file, not assumed — and the `Dockerfile`'s
  `deps`/`runtime` stage runs `uv sync --locked --no-install-project
  --no-dev`, which excludes it. This is latent, not yet observed breakage:
  Phase 0's manifest-exchange route is operator-driven and low-traffic
  enough that this may not have been exercised against a real
  runtime-image deployment yet, but it means the built runtime image is
  already broken for that route today, and every new production `httpx`
  caller this phase and Phase 7 add (`auth.py`, `diff.py`, `posting.py`)
  would ship the same way if left uncorrected. Fixed here, not deferred:
  `httpx` moves to `pyproject.toml`'s main `dependencies` in this phase's
  own PR, alongside `pyjwt[crypto]` above.
- **New bootstrap config, moved here from a later phase — sequencing
  correction:** `apps/worker/settings.py` gains `github_app_id: int` and
  `github_app_private_key: SecretStr` (the fields `.env.example`'s
  existing comment on this exact gap said a later phase would add "when
  real GitHub API calls under the App's identity" exist — this is the
  first such phase, not Phase 7 as an earlier draft of this plan had it;
  `adapter/github/auth.py`'s own JWT-signing code is also built in this
  phase — see the dedicated bullet below — so the bootstrap config it
  needs to run exists in the same PR that first wires it up and deploys
  it).
  `SecretStr` for the private key, per `architecture.md`'s
  credential-handling rule, same as every other secret field in this repo.
  Startup validation (extending Phase 2's fail-fast check, same
  pure-check-function-plus-`SystemExit(1)` shape as `llmgateway/startup_check.py`
  and `adapter/storage/pgvector_check.py`) confirms the configured private
  key is usable by attempting one real `jwt.encode(..., algorithm="RS256")`
  call with it — a single reused call into `pyjwt`/`cryptography`'s own
  signing path, which already performs PEM parsing internally, catching
  whatever exception that raises, rather than a separate hand-rolled
  `cryptography.hazmat...load_pem_private_key` parse step that would
  duplicate validation `pyjwt` already does — before the Worker accepts
  jobs: a malformed/truncated key (e.g. a Helm value copy-paste or
  newline-escaping error) fails loudly at startup, not silently on the
  first real job's GitHub write. Helm chart changes for these two fields
  (`charts/review/templates/configmap.yaml`/`secret.yaml`/`values.yaml`,
  per the Security review's correction on `secret.yaml`'s explicit-per-field
  shape) also move here — this phase is what actually needs them
  deployed, not Phase 7. `docker-compose.yml`'s `worker` service block
  also needs `REVIEW_WORKER_GITHUB_APP_ID`/`REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY`
  added to its own `environment:` section, required-with-no-default via the
  same `${VAR:?set VAR in .env, see .env.example}` syntax the block already
  uses for `REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET` — without it, these
  now-required `WorkerSettings` fields have no way to reach the container,
  and the Worker crash-loops under `restart: unless-stopped` (Phase 3) on
  every `docker compose up`, including this phase's own and Phase 7's E2E
  tests that exercise JWT signing/token exchange through the Compose stack.
- **New `adapter/github/auth.py` — moved here from Phase 7, per this
  phase's own "Why here" correction above:** signs a JWT with the App's
  private key (RS256, `iat`/`exp`/`iss` claims per GitHub's documented
  App-auth flow) and exchanges it for an installation access token
  (`POST /app/installations/{id}/access_tokens`, explicitly timeout-bound
  per `architecture.md`), mirroring `adapter/github/manifest.py`'s
  `exchange_temporary_code` structurally (pure JWT-signing function + one
  `httpx` I/O function, typed exceptions via a new `GitHubTokenExchangeError`
  family in `adapter/github/exceptions.py`). The exchange requests scoped
  to just the job's target repo via the endpoint's `repositories` field (an
  array of repo **names**, e.g. `["review"]` — not `repository_ids`, which
  needs a numeric GitHub repo id this plan never captures anywhere;
  `repository.full_name`, already parsed from the webhook envelope in Phase
  1, splits directly into the owner/repo-name pair this field needs), not
  the full installation — a least-privilege narrowing (a future
  logging/leak bug involving this token exposes write access to one repo,
  not every repo the installation covers) that costs nothing since the job
  already knows its target repo name before requesting the token. **Worth
  noting, though it doesn't need the rate limiter's pre-activation gate —
  the only one of this plan's three named exceptions that's actually a
  product/cost-policy tradeoff, per the distinction in "Assumptions this
  plan makes" above — a distinction worth making explicit rather than
  leaving the categories to look alike:** neither PRD asks for this
  narrowing; it's a self-flagged, cheap addition in the same spirit as
  Gitleaks' `--redact` flag (Phase 5), not a decision reversal. Unlike the
  rate limiter — which changes externally-visible behavior a PRD
  stakeholder might reasonably disagree with (added cost-exhaustion
  mitigation traded against bounding legitimate usage) — this narrowing is
  pure hardening: it strictly reduces the token's blast radius and cannot
  break anything either PRD actually asked for, so shipping it
  unconditionally rather than gating it behind a confirmation step carries
  none of the same risk. (The retry/backoff policy and `TextRedactor` also
  ship unconditionally, for the related-but-distinct reason that they're
  baseline engineering practice rather than a product/cost-policy
  tradeoff — see "Assumptions this plan makes" for that distinction; this
  narrowing doesn't even need that argument, since it's pure hardening
  with no externally-visible tradeoff at all.) Because tokens are now
  repo-scoped, not installation-scoped, the cache is keyed by
  **`(installation_id, repo_id)`**, not `installation_id` alone — caching
  only by `installation_id` would risk reusing one repo's token for a
  different repo under the same installation, defeating the least-privilege
  narrowing this same bullet just introduced. Each cache entry has its own
  `expires_at`, re-exchanged once stale — never a single shared token. The
  cache is instance state owned by the `adapter/github/auth.py` client,
  constructed once at the composition root (`apps/worker/main.py`) and
  passed down via constructor injection, exactly like the existing
  `LiteLLMClient` wiring — never a module-level dict, per `architecture.md`'s
  "no module-level singletons or global mutable state." **Correction to an
  earlier draft's "claimed concurrently" framing, caught by this plan's own
  review:** Phase 3's dispatch loop processes jobs strictly sequentially
  within one Worker instance (one job claimed, fully processed, then the
  next — never two jobs in flight in the same process). The keying still
  matters without any concurrency, though: a single long-lived Worker
  process handles many different installations'/repos' jobs one after
  another over its lifetime, since `JobQueueRepository.claim_next()` isn't
  installation- or repo-scoped, and an under-keyed cache would risk reusing
  one repo's token for a different repo's job processed later by that same
  instance — a cross-tenant correctness/security bug, not just a
  cache-efficiency detail, even with zero actual concurrency. Genuine
  concurrent job processing does happen, but only *across* Worker replicas
  (Helm's `worker.replicaCount`, default `1` but not prevented from being
  raised for throughput) — each replica constructs its own independent
  cache via the same composition-root wiring, so that case is already
  handled correctly by construction, not as an afterthought. No persistence
  (matches the existing "credentials aren't written to Postgres" split).
  **Unbounded cache growth, flagged the same way Phase 1's own
  `webhook_delivery` table growth is flagged, not silently left
  unaddressed:** the `(installation_id, repo_id)`-keyed cache has no
  eviction policy — a long-lived Worker replica processing jobs across many
  distinct installations/repos over its uptime accumulates cache entries
  indefinitely; each entry is harmless once past its own `expires_at` (the
  token itself stops working GitHub-side), but the entry and its metadata
  are never purged from process memory. Low severity relative to
  `webhook_delivery`'s unbounded Postgres growth (this cache is in-memory
  and reset on every Worker restart/redeploy, not a persistent artifact),
  but the same class of gap — deferred to the same Phase 8
  pruning/hardening bucket as that table, not addressed in this phase. **GitHub-side transient
  failures, addressed rather than left uniformly fatal — one shared
  implementation, not three independent copies:** a 429 or
  secondary-rate-limit response honors `Retry-After` **up to a maximum cap
  (10 seconds) before giving up rather than waiting** — an unbounded
  wait on a GitHub-supplied value would let GitHub's own response dictate
  how long a Worker job occupies its slot; a bare 5xx retries with a short
  bounded backoff (up to 3 attempts, short exponential — roughly 1s/2s/4s).
  **Sized against the smallest per-step timeout that uses this helper, not
  an arbitrary round number — a single-call bound, not a per-step
  guarantee; see "Interaction with the per-step timeout" below for why a
  step making more than one call still can't overrun its own budget:**
  pre-filter and the output step both default
  to `30` seconds (Phase 2), the smallest of the four macro-step budgets,
  and both call into this shared helper (pre-filter for the Files/Trees
  calls, output for posting); a single call's worst case (`10`-second
  `Retry-After` cap plus ~`7` seconds of backoff, ≈`17` seconds) stays
  comfortably under that `30`-second floor, leaving headroom for that one
  call within the step's own work — pre-filter and output can each still
  make more than one such call in a single job (e.g. pre-filter's Files
  and Trees calls), so this figure bounds one call's worst case, not the
  step's total; the step's own configured timeout, not a sum of these
  per-call figures, is what actually bounds a multi-call step, per the
  paragraph below. Checked against the reuse order
  first: `httpx` (the client every `adapter/github` call already uses) has
  no built-in status-code-aware or `Retry-After`-aware retry (its own
  `transport=httpx.HTTPTransport(retries=n)` covers only connection-level
  errors before a response is received, not HTTP-level 429/5xx responses),
  and no `tenacity`/`backoff`-style dependency exists anywhere in this
  repo — genuinely new, not a reuse gap. Implemented **once**, in a new
  `adapter/github/http.py` shared helper (a thin wrapper around the
  `httpx.AsyncClient` every `adapter/github` module already constructs),
  not independently in `auth.py`, `diff.py`, and `posting.py` — the same
  "build once, reuse from every consumer" discipline this plan already
  applies to `TextRedactor` (Phase 5) rather than three divergent retry
  implementations. This shared helper is also where the `Authorization`
  header gets suppressed from ever reaching a log line — **safely, not by
  touching the live request:** an `httpx` request `event_hook` runs on the
  actual `Request` object before it's transmitted, so mutating
  `request.headers["Authorization"]` in place to redact it would send the
  corrupted value to GitHub itself and break every GitHub write this
  helper exists to make. Instead, this helper's own logging call sites
  build their log record from a redacted **copy** of the relevant fields
  (attempt number, target URL, status/reason) that never includes
  `request.headers` at all, rather than logging the `Request`/`Response`
  object itself and relying on any hook to have first scrubbed it — the
  live object passed to `httpx`'s transport is never touched. A single
  choke point, since every `adapter/github` call already routes through
  this client, except `adapter/github/manifest.py`'s pre-existing
  `exchange_temporary_code` (Phase 0), which constructs its own separate
  `httpx.AsyncClient` and doesn't route through this helper — currently
  harmless from a redaction standpoint, since that call sends no
  `Authorization` header, but it also means that one call gets none of this
  helper's bounded retry/backoff either, a real (low-stakes) reuse gap this
  plan leaves as-is rather than migrating a one-time, operator-driven,
  Phase-0 code path outside this phase's own scope; noted here rather than
  left as an unqualified "every call" claim. Every retry attempt logs at
  `info` with the call's
  target, attempt number, and reason (`rate_limited`/`server_error`) before
  retrying, not only the final success/failure, so an operator can tell
  "GitHub was flaky but recovered" from silence rather than only from the
  absence of a fatal log line. **This `info` log is a distinct signal from
  the `warning` this same helper's routine-failure outcome gets, per
  Observability & rollback below — named explicitly here so the two don't
  read as contradicting each other:** the `info` line above fires per
  attempt, while retries are still genuinely in flight and expected to
  recover; the `warning` fires once, only if a call exhausts its entire
  retry budget (or a 429 exceeds the `Retry-After` cap) and the call
  ultimately fails because of it — a distinct, less common event from an
  individual attempt being retried, and the one "routine, transient GitHub
  failures... log at `warning`" actually refers to. **Scoped away from `posting.py`'s writes,
  not applied uniformly:** `auth.py`'s token exchange (a `POST`) and
  `diff.py`'s read-only `GET` calls are safe to retry on any 429/5xx —
  `auth.py`'s case named explicitly rather than left grouped with
  `diff.py`'s `GET`s without its own reasoning, since it's also a `POST`
  and might otherwise look like an unexplained exception to `posting.py`'s
  idempotency scoping below: retrying a token exchange after an ambiguous
  timeout or bare 5xx risks minting an extra valid installation access
  token, not duplicating any customer-visible content — a materially lower
  risk than `posting.py`'s writes, which is why it's treated as
  unconditionally safe here rather than subjected to the same
  idempotency-scoped analysis. `posting.py`'s `POST` calls (create PR review, create check-run) are not
  documented as idempotent by GitHub's API — retrying a `POST` after a
  timeout or a bare 5xx where the request may already have been processed
  server-side risks a duplicate PR review or check-run landing on the
  customer's repo. `posting.py`'s calls therefore only retry on responses
  that are unambiguously safe to retry before GitHub could have processed
  the write (429/secondary-rate-limit, which GitHub returns without
  executing the request, and connection-level failures before any bytes
  were sent) — never after an ambiguous timeout or a bare 5xx received
  after the request was actually transmitted, where a duplicate post is a
  real risk; either of those fails fatally on the first occurrence instead.
  **Interaction with the per-step timeout (Phase 2) — the timeout is the
  actual backstop, not a budget this bullet's numbers are separately sized
  to fit under, corrected from an earlier draft's unqualified "small enough
  relative to" claim:** retries add to, not replace, the time a
  GitHub-calling step takes, and the static-analysis step's own
  content-fetch phase (Phase 5, calling `fetch_content()` on this phase's
  `DiffFetcher`) can involve up to ~50 sequential/batched Blob-fetch calls
  (the 500-file materialization ceiling divided by the 10-way bounded
  concurrency above), each individually eligible for its own retry budget
  — worst-case compounding delay across that many calls is not something
  this bullet's per-call caps alone bound, and this plan has no reliable
  way to provision a retry budget that's guaranteed to fit under that
  step's `static_analysis_step_timeout_seconds` in every case given
  real-world GitHub latency variance. The actual contract is stated
  plainly instead: each step's own configured timeout (Phase 2's
  `StepTimeouts`) is the backstop. If a GitHub-calling step —
  including whatever retries are still in flight when the deadline arrives
  — has not completed within its own timeout, `asyncio.wait_for` cancels it
  and the step fails the same fatal way as any other step timeout (Phase
  3's `FAILED` transition), not via "retries exhausted." The per-call caps
  in this bullet (at most 3 backoff attempts; at most one `Retry-After`
  wait, capped at 10 seconds — well under even pre-filter's and output's
  30-second timeouts, the smallest of the four) still
  matter — they keep a *single* call's worst-case wait well short of the
  step budget, so one ordinary transient hiccup doesn't alone consume the
  whole step's time — but the timeout, not this bullet's arithmetic, is
  what actually bounds worst-case job duration under repeated transient
  failures. Still fatal (the job fails, per Phase 3) once retries are
  exhausted, the step timeout fires first, or the response is a permanent
  error (401/403/404) — this is a minimal, in-scope allowance for ordinary
  transient GitHub hiccups, not a circuit breaker or full resilience layer,
  which stays out of scope for a walking skeleton. Retry exhaustion (a bare
  5xx that never recovers, or a 429 whose `Retry-After` exceeds the cap)
  logs at `warning` before the exception propagates — the "routine,
  transient GitHub failures... log at `warning`" outcome Observability &
  rollback below refers to, distinct from the per-attempt `info` log
  above. A permanent error (401/403/404) and a step-timeout cancellation
  are each already covered by their own existing fatal-exception handling
  and are not additionally logged at `warning` here — a permanent error is
  never "routine" the way a transient one is, and a step timeout is
  Phase 2's own mechanism to log, not this helper's.
- New `review/prefilter.py`: a `Step` implementation (per Phase 2's
  Protocol) taking the changed-file list from `ingestion/diff.py`'s
  fetched `Diff`, returning the subset that should proceed. Default ignore
  patterns (lockfiles, vendored paths, generated files, binary files) as a
  plain Python constant — no bootstrap-config override in this phase (the
  PRD marks operator-configurable ignore patterns Could-have, not
  Must-have). **Accepted limitation, named rather than left implicit:**
  binary-file exclusion here is extension/pattern-based (matching on
  filename), not content-based — a compiled binary or archive committed
  under a non-ignored extension bypasses this filter and reaches Gitleaks,
  Semgrep, and the LLM diff prompt like any other file. Bounded downstream
  by this plan's existing size/count materialization ceilings (Phase 4) and
  per-step timeouts rather than caught here; a content-sniffing filter
  (magic-byte detection) is a reasonable Phase 8 enhancement, not built in
  this phase.
- **File materialization safety, correction — content-fetching stays in
  `adapter/github`, not `adapter/analyzers`; disk-writing is
  `adapter/analyzers`' job, not `adapter/github`'s:** a first draft of
  this bullet had the static-analysis macro step (`review/`, a domain
  package) writing fetched file content to disk itself — filesystem I/O
  is infra, and `architecture.md` is explicit that domain packages carry
  zero infra imports. A second draft moved the fix too far the other
  way, having `adapter/analyzers/subprocess_runner.py` call GitHub's Git
  Blobs API directly — this plan's own review caught that as breaking the
  one boundary it otherwise holds firmly: every GitHub-API call in this
  plan (diff-fetch, token exchange, posting) lives in `adapter/github`,
  the one package that owns "how this platform talks to GitHub," and
  `adapter/analyzers` has no installation-token access wired to it
  anywhere. Corrected split: **`adapter/github/diff.py`** (already built
  in this phase) fetches each pre-filtered file's actual content via
  `GET /repos/{owner}/{repo}/git/blobs/{blob_sha}` (the Git Blobs API, one
  call per file that *survives* `review/prefilter.py`, not one per changed
  file in the whole PR — deliberately sequenced after pre-filter, not
  before, since fetching every changed file's content up front would pay
  a per-file GitHub API call for exactly the lockfiles/vendored/generated/
  binary files pre-filter exists to make cheap to skip) — using the same
  installation token `adapter/github/diff.py` already holds for the
  Files/Trees calls above, no new token-provider wiring needed. This
  extends `ingestion/diff.py`'s `DiffFetcher` Protocol with a second
  method, `fetch_content(pre_filtered_paths) -> dict[path, bytes]`, called
  by `review/static_analysis.py` (the domain step, via the Protocol it
  already depends on) after pre-filter completes. **`adapter/analyzers/subprocess_runner.py`**
  then does only what an analyzer adapter should: given those bytes (not
  a GitHub call of its own), it creates a per-invocation
  `tempfile.TemporaryDirectory`, writes each file, runs the tool, and
  tears the directory down in a `finally` block — success or failure —
  never into a shared or predictable path. Blob fetches are keyed by SHA,
  never by path, which sidesteps any need to URL-encode a PR-controlled
  filename into the fetch request (materialization-time path handling,
  below, is a separate concern from the fetch). A `removed`-status file
  (no `blob_sha`) is never fetched — it no longer exists at the head
  commit, so there's nothing to materialize or scan. Every PR-controlled
  filename is validated before it is joined onto the temp-directory path:
  rejected outright if it contains a `..` path segment, resolves outside
  the temp directory once normalized, or is an absolute path — logged at
  `warning` with the rejected filename, since a path-traversal attempt is
  itself worth an operator seeing, the same as the symlink/submodule case
  below. **Symlink escape, addressed as its own mechanism, not just a
  filename check:** a `Diff` entry can itself describe a symlink (git mode
  `120000`, target chosen by the PR author); filename-string validation
  alone doesn't stop a symlinked entry from being written and then
  followed outside the temp directory by whatever reads it next.
  `adapter/github/diff.py` therefore checks each `Diff` entry's real git
  `mode` (from the Trees-API cross-reference above) **before fetching its
  blob content at all**: a `120000` (symlink) or `160000` (submodule)
  entry is **skipped entirely — no blob fetch, no file written, no
  placeholder either** — and logged at `info` as excluded, distinct from
  a normal `100644`/`100755` (regular file/executable) entry, which
  **is** fetched and written, as an ordinary regular file, using
  `O_CREAT | O_EXCL` (never following an existing path at the target,
  never creating a symlink itself; the executable bit itself is not
  preserved — neither Gitleaks nor Semgrep's scanning behavior depends on
  it). A PR is attacker-controlled input, and its file list is the first
  PR-controlled data this platform writes to the Worker's local
  filesystem.
- **`WorkerSettings` gains `github_api_base_url`/`github_api_timeout_seconds`,
  reusing the pattern `apps/webhook_api/settings.py`'s `WebhookAPISettings`
  already established for exactly this purpose** (consumed today by
  `adapter/github/manifest.py`'s `exchange_temporary_code`) — every new
  Worker-side GitHub-calling site this phase and Phase 6/7 add
  (`adapter/github/auth.py`, `diff.py`, `posting.py`) shares these two
  fields rather than each hardcoding its own base URL/timeout, matching
  this plan's own "explicitly timeout-bound per `architecture.md`"
  requirement with an actual config source instead of an unnamed literal.
  **Default stated explicitly, not left as the one numeric bootstrap-config
  field in this plan without one:** `github_api_timeout_seconds` defaults
  to `10.0`, matching `WebhookAPISettings.github_api_timeout_seconds`'s
  existing value exactly (`apps/webhook_api/settings.py`) — the Worker's
  copy of this field carries the same default as the one Phase 0 already
  established for the same kind of call, not a new, independently-chosen
  value. A third field, `github_retry_max_attempts: int = 3`, is added
  alongside these two — the retry/backoff policy's own configurable lever
  (see "Assumptions this plan makes" for why this is a parameterized
  count rather than a boolean flag, and why `0` achieves the same effect
  as disabling it wholesale).
- **Coverage-limit and cost-bounding failure modes, specified rather
  than left implicit — three distinct GitHub API ceilings this round's
  diff-fetch redesign runs into, none of which the plan addressed until
  this review caught them:**
  - The Files-endpoint (`GET .../pulls/{pull_number}/files`) caps at
    3,000 files per PR regardless of pagination. `adapter/github/diff.py`
    compares the number of files actually returned against the PR
    object's own `changed_files` count (fetched via the existing
    `GET .../pulls/{pull_number}` call this plan already makes for other
    metadata); a mismatch is a typed, fatal exception — never a silent
    truncation, since an attacker could otherwise pad a PR past the cap
    to push files past review entirely.
  - The Git Trees API (`GET .../git/trees/{sha}?recursive=1`) truncates
    at 100,000 entries / 7MB and sets `truncated: true`, with no
    pagination available for it. `adapter/github/diff.py` is fatal only
    when a changed-file path from the Files-endpoint response has **no**
    matching Trees-API entry to cross-reference for its `mode` — whether
    that's because `truncated: true` cut it off or any other reason —
    never merely because `truncated` is set while every changed-file path
    was still resolved before truncation occurred (the common case for an
    ordinary PR against a large monorepo, where the *changed* files are
    almost always near the start of tree traversal and the truncation
    affects only the long tail of unrelated, unchanged paths). Silently
    defaulting a genuinely missing entry to "regular file" would let a
    truncated response's missing symlink entries bypass the guard above
    entirely, the exact failure mode this guard exists to prevent — that
    risk is scoped to missing *needed* entries specifically, not to
    truncation as such. **Known scale limitation, flagged rather than
    silently accepted:** this endpoint is called in full (`recursive=1`,
    the whole tree) on every review including every `synchronize` push,
    purely to resolve `mode` for the handful of changed files — for a
    genuinely enormous monorepo where even the changed-file paths
    themselves fall past the 100,000-entry/7MB cutoff, that changed file's
    mode is unresolvable and the review fails outright with no fallback.
    A per-path mode lookup (avoiding the full-tree fetch entirely) is a
    real alternative but adds one Git-Trees-API-shaped call per changed
    file instead of one call per PR — a tradeoff this plan defers to
    Phase 8 as a named follow-up rather than solving here, since the
    common case (a real PR's changed files resolve well within the
    tree-traversal order that precedes truncation) is the one Phase 1
    optimizes for. **Decoupled from the per-repo rate limiter's own cost
    dimension, worth naming explicitly rather than conflating the two:**
    Phase 1's `review_enqueue_rate_limit_per_minute` bounds enqueue
    *frequency*, not the *size* of any single review this call is part of —
    a repo within the frequency budget can still trigger this full-tree
    fetch on every one of those enqueued reviews regardless of the repo's
    tree size, so a future Phase 8 fix for this scale limitation should
    account for both dimensions (how often, and how large each one costs)
    rather than assuming the rate limiter already bounds this call's own
    cost.
  - Blob-fetch volume has no natural ceiling from pre-filter alone (it
    excludes by pattern, not count) — a PR crafted with hundreds of small,
    non-ignored files could drive a correspondingly large number of
    sequential Blob API calls in one review, a per-review cost/availability
    vector distinct from the per-*trigger* rate limiting Phase 1 already
    adds. `review/prefilter.py` caps the number of files it passes
    through to materialization at a fixed ceiling (e.g. 500); files beyond
    it are dropped from this review with a `warning` log naming the count
    dropped — the same "flag, don't silently truncate coverage" posture
    as the two ceilings above, but bounding cost rather than closing a
    bypass, so a capped-and-logged drop (not a fatal failure) is the right
    tradeoff here. The up-to-500 per-file Blob fetches this ceiling still
    allows run at a small, fixed bounded concurrency (e.g. 10 concurrent
    requests, enforced with a stdlib `asyncio.Semaphore` — no new
    dependency — matching the restraint this plan shows elsewhere for
    GitHub API usage) rather than either fully sequential (unacceptable
    latency for a review-sized PR) or fully unbounded (risking GitHub's
    secondary rate limits) — the same `httpx` client this bullet's sibling
    calls already share.
  - **Aggregate-bytes ceiling, closing a cross-tenant disk-exhaustion gap
    this plan's own review caught — and correcting an earlier draft's
    "concurrently-running jobs on the same pod" framing, which doesn't
    actually apply given Phase 3's strictly-sequential per-instance dispatch
    loop:** the 500-file materialization ceiling
    bounds file *count*, not aggregate bytes — each surviving file can
    still be up to the Git Blobs API's ~100MB per-blob ceiling, and Phase
    5 doubles this by materializing a separate copy per analyzer sub-step.
    The real cross-tenant vector isn't two jobs sharing one pod
    concurrently (Phase 3 never does that) — it's a Kubernetes-node-level
    one: multiple Worker replicas (separate pods, each independently and
    sequentially processing a different installation's/repo's jobs —
    `worker.replicaCount`, default `1` but not prevented from being
    raised) commonly land on the same underlying node and share that
    node's ephemeral disk unless the deployment dedicates separate storage
    per pod, which this plan doesn't specify. A crafted PR (many large
    non-ignored files, each individually under any per-file ceiling)
    materializing unbounded aggregate bytes into one pod's temp
    directories during its own job's processing can exhaust the node's
    disk, causing `ENOSPC`/eviction pressure for a completely different
    tenant's Worker pod scheduled to the same node — a cross-tenant
    denial-of-service distinct from the per-file/per-count ceilings above,
    real even though no two jobs ever run at once in the same process.
    `review/prefilter.py` therefore
    also caps the **aggregate** materialized-content byte size it will
    pass through (e.g. 200MB, sized well under the container disk budget
    Phase 5's `mem_limit`/`cpus` figures assume alongside this) —
    files are dropped in the same order the file-count ceiling already
    orders them once the running total of each surviving file's
    `Diff.blob_size_bytes` (above) would exceed it, logged at
    `warning` with the byte total dropped, the same capped-and-logged
    posture as the file-count ceiling next to it, not a fatal failure.
    Running this check against `blob_size_bytes` rather than actual
    fetched content is also what keeps pre-filter scoped to `fetch()`
    only, never calling `fetch_content()` itself (Phase 5's job) — the
    ceiling estimates aggregate bytes from data `fetch()` already returns.
  Additionally, the Git Blobs API has a documented ~100MB size ceiling; a
  blob at or beyond it returns an error rather than truncated/silent
  content. Treated the
  same as any other GitHub API failure in this plan — a typed exception
  that fails the job (Phase 3's `FAILED` transition) — never a silently
  skipped/empty file, which would let an oversized file hide a secret or
  vulnerable pattern from the very tools this platform depends on as its
  static-analysis trust anchor. This is a real, if narrow, availability
  tradeoff (one huge changed file fails an otherwise-reviewable PR) that
  favors safety over completeness for Phase 1; a size-aware fallback
  (e.g. skip-and-flag instead of fail) is left as a Phase 8 refinement.

**Tests:** Unit (per `docs/adr/0005-pure-no-io-logic-unit-tested.md` and
the PRD's own Testing table — US3 is Unit-only per the PRD itself, and per
the ADR that formalizes why a pure-filtering story with no I/O boundary
can't structurally have an integration test): US3's `Given/When/Then`
(`package-lock.json` filtered, `app.py` not) plus each default pattern
category (vendored path, generated file, binary file) as its own case,
plus a case asserting the fixed materialization ceiling (e.g. 500) drops
excess files with a `warning` log rather than fetching them (boundary
cases: exactly 500 surviving files all pass through, 501 drops exactly
one), plus a
companion case asserting the aggregate-bytes ceiling (e.g. 200MB) drops
files once the running total of the fake `DiffFetcher`'s returned
`Diff.blob_size_bytes` values would exceed it, also logged at `warning`
with the byte total dropped (boundary cases: a running total landing
exactly at 200MB passes, one byte over drops the file that would have
crossed it), in `review/test_prefilter_step.py`, against a
fake `DiffFetcher`. Integration
(per `testing.md`'s "GitHub adapter against a recorded cassette"
convention): `adapter/github/diff.py`'s fetch calls (the paginated
Files-endpoint call, the Trees-API call, and the per-surviving-file
Git-Blobs-API call) against a recorded GitHub API cassette (`respx`), in
`adapter/github/test_diff.py`, including: a negative case for an
unreachable/malformed diff-fetch response, asserting both the typed fatal
exception and that it logs at `error`, per Phase 2's macro-step-fatal
default; a case asserting a second page
of Files-endpoint results is actually followed and merged; a case
asserting a Files-endpoint response whose count disagrees with the PR
object's own `changed_files` count fails fatally rather than silently
truncating (the 3,000-file-cap guard); a case asserting a Trees-API
response with `truncated: true` where every changed-file path is still
resolved (accepted, not fatal, proving the narrowed truncation-fatal
condition above), a separate case with `truncated: true` **and** a
changed-file path absent from the Trees-API response (fatal), and a
third case with a Files-endpoint path absent from a non-truncated
Trees-API response (also fatal), so the "fatal only when a needed entry is
actually missing" distinction is tested on both its accept and reject
sides, not defaulting the missing entry's mode to "regular file" (the
guard's own security property, tested directly, not only inferred from
the symlink-skip case below) — each of these three fatal cases also
asserts the log fires at `error`, not just that the typed exception is
raised, locking in the level this plan's Observability & rollback
subsection depends on for security-relevant filtering; a symlink-mode
(`120000`) and a submodule-mode (`160000`) `Diff` entry are both skipped —
no blob fetched — rather than materialized; a Blob fetch response at/over
the Git Blobs API's ~100MB size ceiling asserts the same
typed-fatal-exception behavior (including the `error`-level log) as the
two coverage-limit guards above, closing the one ceiling in this phase's
Assumptions that was previously asserted in prose only; a
case asserting the bounded blob-fetch concurrency cap (e.g. 10) is
actually enforced — a cassette handler that records the maximum number of
concurrent in-flight Blob requests observed during the test and asserts it
never exceeds the configured cap, using a fixture PR with enough surviving
files to exceed the cap if concurrency were unbounded. Unit (per the same
ADR — pure
path-string guard logic with no I/O of its own, tested at Unit tier
despite living inside `adapter/analyzers`, in
`adapter/analyzers/test_subprocess_runner.py`): the path-traversal guard
(`..` segment, absolute path, resolves-outside) rejects each malicious
case and accepts a normal relative path, given already-fetched bytes (a
fake `dict[path, bytes]`, not a live GitHub call — the mode-based
symlink/submodule guard itself now lives in, and is tested in,
`adapter/github/test_diff.py` above, since that's where the fetch
decision now happens). The private-key startup validation (moved here from a
later phase, above) gets its own test in `apps/worker/test_settings.py`:
a valid test RSA key passes; a malformed/truncated key fails startup with
`SystemExit(1)`, the same shape as the existing pgvector/LLM
startup-check tests. Integration (per `testing.md`'s recorded-cassette
convention, in `adapter/github/test_auth.py` — **moved here from Phase 7,
per this phase's own "Why here" correction:** `adapter/github/auth.py` is
built and consumed in this phase's own PR, so its tests land in the same
PR, not three phases later): JWT-sign-and-exchange against a recorded
cassette; a negative case for an unreachable/malformed token-exchange
response, typed per the new `GitHubTokenExchangeError` family; a case
asserting the `(installation_id, repo_id)` cache is not reused across two
different repos under the same installation; a case with a cache entry
whose `expires_at` is in the past asserting it triggers a fresh
JWT-sign-and-exchange rather than returning the stale token. The shared retry/backoff
behavior itself is tested once, directly, against the new
`adapter/github/http.py` helper, in `adapter/github/test_http.py`: a 429
response with `Retry-After` is honored up to the cap and eventually
succeeds; a `Retry-After` value exceeding the cap fails without waiting
the full duration; a bare 5xx is retried with backoff and eventually
succeeds; a bare 5xx that keeps failing exhausts its retry budget (up to 3
attempts) before being surfaced as fatal, asserting the exhaustion itself
logs at `warning` (not the `info` an in-flight attempt gets, and not the
`error` a permanent failure gets) — the case that locks in the three-way
log-level distinction the Observability & rollback subsection below
states; a permanent error (401/403/404)
is surfaced as fatal on the **first** response, with no retry attempted at
all — a distinct branch from the retry-exhaustion case, since a permanent
error is never expected to succeed on a later attempt and retrying it
would only waste the step's time budget — and asserts this case logs at
`error`, not the `warning` the retry-exhaustion case gets, completing the
three-way distinction's own test coverage; each attempt is asserted to log
at `info` with the attempt number and reason. **`github_retry_max_attempts=0`
case, added by this pass to prove the "disables it wholesale" claim rather
than leaving it asserted but untested:** with the helper configured at
`0`, a would-be-retryable 429 response and a would-be-retryable bare 5xx
each fail immediately on the first response — no `Retry-After` wait, no
backoff loop, zero `info`-level attempt logs — proving both retry paths
this helper implements are actually gated by the same counter, not only
the bare-5xx path. **`Authorization`-header suppression, proven rather than
only claimed in the Security review — and proven safe, not only present:**
a case makes a request carrying a real `Authorization` header through the
shared client against a stub GitHub endpoint (`respx`), with logging
captured, and asserts BOTH properties together: (a) the captured log
record never contains the raw header value, only the fixed redaction
marker, and (b) the stub endpoint itself received the correct, unredacted
`Authorization` header and the call succeeded — proving the redaction
happens only in the logging path, not by mutating the request actually
sent to GitHub. Asserting (a) alone, as an earlier draft of this case did,
would leave the mechanism's one catastrophic failure mode (redacting the
live request, breaking every GitHub write) unverified either way. A case in `apps/worker/test_main.py` (the same
composition-root test file Phase 5 uses for its own conditional-logging
assertions) asserts the one-time `info`-level startup log fires exactly
once at Worker boot when `github_retry_max_attempts=0`, locking in the
third leg of this plan's three-way startup-log-level distinction the same
way the rate limiter's and redaction's own levels are already locked in.
`adapter/github/test_auth.py` above and
`adapter/github/test_diff.py` (below) each add one case confirming their
own call site actually goes through the shared helper (asserting the
helper is invoked, not re-testing the retry logic itself) — mirroring how
Phase 5's `TextRedactor` is tested once at its concrete implementation and
reused elsewhere, not re-verified per call site; Phase 7's own Tests
subsection adds the equivalent case for `posting.py`, including the
non-idempotent-write retry-scoping behavior specific to that call site.

**Observability & rollback:** Logs the count filtered vs. passed through,
at `info`, per job, plus the fetched-diff file count and byte size. A
one-time startup log (`apps/worker/main.py`, alongside the existing
startup-check logging) fires at `info` when an operator has set
`github_retry_max_attempts=0`, stating that GitHub API retries are
disabled (see "Assumptions this plan makes" for why this is `info`, not
the rate limiter's own `warning`).
Logging levels follow one consistent convention across this plan:
**routine, transient GitHub failures that the retry/backoff policy already
absorbs and often self-heals from** (Phase 4's own `adapter/github/http.py`
helper — a bare 5xx or rate-limit response, retried and typically
recovering) **log at `warning`** — specifically the retry-exhaustion
outcome (that same helper's own bullet above states this precisely), not
each individual retry attempt in flight, which logs at `info` as ordinary
progress, the every-day noise floor an operator expects. **Only a
confirmed, non-retryable problem logs at `error`:** the three anti-bypass
**fatal** exceptions here — Files-endpoint count mismatch, Trees-API
missing-needed-entry, and the Git-Blobs-API size ceiling (~100MB) — are a
detected tampering/bypass-probe signal, and Phase 7's partial-post defect
(below) is a confirmed application-level defect; both represent something
an operator should actually act on, not routine flakiness, so both log at
`error`, with a distinct exception type/reason string carried too so an
operator can further tell "someone is probing the bypass guards" apart
from other `error`-level causes once looking. The materialization ceiling
and the aggregate-bytes ceiling are not part of that trio: both are
non-fatal, capped-and-logged drops (per their own bullets above), logged
at `warning` with the count/byte total dropped, same as every other
silent-drop ceiling in this plan. Rollback: no
state beyond redeploy — pre-filter itself is a pure function;
a `DiffFetcher` failure (token exchange, GitHub API error, or a blob over
the Git Blobs API's size ceiling) is a typed exception that fails the job
(marked `FAILED` per Phase 3; once Phase 7 lands, also posts a
failure-conclusion check-run per that phase's US8 fix — this phase's own
failure path is unchanged by that later addition), never a
partial/garbled pre-filter result.

**Acceptance:** the Unit and Integration tests above pass; registering
`review/prefilter.py` in Phase 2's registry required zero changes to
`review/step.py` or `review/pipeline.py` (the mechanical proof of US-F3's
macro-step extensibility claim, now exercised by a real step instead of
only a fake one); `mypy --strict` confirms `review/prefilter.py` imports
nothing from `adapter/github` directly (only the `DiffFetcher` Protocol);
`docs/adr/0006-protocol-defined-at-data-model.md` (above) exists and is
merged as part of this phase's own PR, not left as a forward promise — the
same "ADR merged before/with the phase's PR" bar Phase 1's own Acceptance
section holds itself to for ADR 0005.

## Phase 5 — Static-analysis step + Gitleaks/Semgrep adapters

**Why here:** depends on Phase 2 (the framework, for both the macro step
and its internal analyzer-sub-step registry) and Phase 4 (consumes the
fetched `Diff` and pre-filtered file list — see Phase 4's file-materialization
note) but not on Phase 6 or 7 — the PRD's Unit E has no dependency on
llm-review or findings-output. Sequenced before Phase 6 (LLM-review)
because static analysis's findings are llm-review's context input (per the
PRD's US5), so its output shape needs to exist first.

- New `review/static_analysis.py`: the macro `Step` implementation, plus a
  `StaticAnalyzer` `typing.Protocol` (defined here, the consumer, per
  `architecture.md`) with one method,
  `run(diff: Diff, content: dict[str, bytes], *, timeout) -> list[Finding]`
  — `diff` is Phase 4's in-memory `Diff` (file paths, modes, and patch
  text) and `content` is each pre-filtered file's actual bytes, both
  passed as data, never a filesystem path; this step passes data, not
  disk state, to the Protocol boundary, keeping the domain package free
  of any filesystem I/O. `review/static_analysis.py` obtains `content` by
  calling `DiffFetcher.fetch_content(pre_filtered_paths)` once — the same
  Protocol Phase 4 defines in `ingestion/diff.py`, constructor-injected
  here exactly as `review/prefilter.py` already depends on it, never a
  direct `adapter/github` import — then passes the resulting
  `dict[path, bytes]` to every analyzer sub-step's `run()` call. Internally
  builds and runs a second Phase-2-registry instance over the two enabled
  analyzer sub-steps, concurrently, isolating failures per
  `pipeline-step-framework.md`'s US-F5. **Headroom between the outer
  macro-step timeout and the inner sub-step timeout, sized deliberately so
  isolation actually holds — otherwise a sub-step timing out at its own
  budget could exhaust the outer step's budget before the macro step
  itself finishes, turning US-F5's non-fatal isolation into a fatal US-F6
  failure instead:** this step's own `static_analysis_step_timeout_seconds`
  default (`240`, Phase 2) budgets for content-fetch, the analyzer
  sub-step phase, and the redaction pass in sequence — the analyzer phase
  itself is bounded by `static_analysis_analyzer_timeout_seconds` (`60`,
  Phase 2, run concurrently via `asyncio.gather` so a sub-step timing out
  costs at most that `60`-second budget, not `60` × 2), leaving well over
  `100` seconds of the outer budget for content-fetch and the redaction
  pass even in the worst healthy case where one analyzer sub-step uses its
  entire budget before being isolated. The concrete `GitleaksAnalyzer`/
  `SemgrepAnalyzer` instances (below) are constructor-injected from the
  composition root (`apps/worker/main.py`), the same as every other
  concrete adapter in this plan — `review/static_analysis.py` never
  imports `adapter/analyzers` directly.
- New `adapter/analyzers/gitleaks.py` and `adapter/analyzers/semgrep.py`
  (today `adapter/analyzers/__init__.py` is only a docstring — this is the
  first real code in that package): each implements `StaticAnalyzer`,
  materializing the `content` bytes it's given to a fresh per-invocation
  `tempfile.TemporaryDirectory` (via the shared
  `adapter/analyzers/subprocess_runner.py` helper — see Phase 4's
  file-materialization note for the path-traversal/symlink guards it
  enforces there, since disk I/O is adapter-layer work, not
  `review/static_analysis.py`'s) before running its tool via
  `asyncio.create_subprocess_exec` (per `architecture.md`'s async-subprocess
  rule), then tearing the directory down in a `finally` block regardless of
  outcome. Each parses its tool's native JSON output and maps it to the
  `findings` domain package's `Finding` pydantic model (new:
  `findings/models.py` — today `findings/__init__.py` is only a docstring
  too). **Subprocess lifecycle on timeout/cancellation:** `asyncio.wait_for`
  cancellation alone does not kill the underlying OS process — cancelling
  the coroutine that's awaiting `process.communicate()` (via the
  sub-step-level `asyncio.wait_for(..., timeout=static_analysis_analyzer_timeout_seconds)`
  from Phase 2) only stops the Python-side await — the spawned Gitleaks or
  Semgrep child process keeps running as an orphan unless something
  explicitly reaps it. `subprocess_runner.py` therefore wraps every
  subprocess call in `try`/`except asyncio.CancelledError`/`finally`: on
  cancellation, it calls `process.kill()` then `await process.wait()` to
  reap the now-dead child before letting the `CancelledError` propagate,
  for every subprocess spawn site this plan has — both analyzer sub-steps
  here, and `TextRedactor`'s own Gitleaks invocations (below). **Bounded
  memory:** `process.communicate()` reads to EOF and accumulates all output
  regardless of any `limit` kwarg (that kwarg only bounds `StreamReader`
  line-based backpressure, not `communicate()`'s total accumulated size), so
  `subprocess_runner.py` does not use `communicate()`; it reads
  `process.stdout` in fixed-size chunks (`process.stdout.read(n)`) in a
  loop, accumulating into a buffer while checking the running byte total
  against a fixed cap (`10MB` — reusing the same order-of-magnitude reasoning as
  this plan's other output-size ceilings; both Gitleaks' and Semgrep's JSON
  output for a review-sized diff is expected to be orders of magnitude
  smaller than this, so the cap exists to catch pathological/adversarial
  output, not to constrain ordinary tool behavior). **The same
  chunk-accumulate-and-cap shape `apps/webhook_api/webhook_routes.py`'s
  existing `_read_body_within_limit` already uses for the webhook request
  body (Phase 0), noted here rather than left as an unremarked coincidence
  this plan's own review caught:** the two aren't consolidated into one
  shared helper, since their underlying read APIs don't actually share a
  signature (`Request.stream()`'s async iterator vs.
  `asyncio.StreamReader.read(n)`'s explicit chunk-size argument) — forcing
  a common abstraction over two APIs that don't line up would be exactly
  the kind of interface-without-a-genuine-second-implementation
  `architecture.md`'s own "no interface without a second real
  implementation already in view" rule warns against, not a missed reuse
  opportunity — if the cap is exceeded before
  the process finishes, it's killed and reaped via the same
  `process.kill()` + `await process.wait()` path above, and the step fails
  with a typed "analyzer output exceeded the size cap" error rather than
  the process being left running or the Worker's memory being exhausted
  reading further. **`stderr`, addressed rather than left unspecified — a
  gap this plan's own review caught in an earlier draft that only ever
  named `stdout`:** `asyncio.create_subprocess_exec` is called with
  `stderr=asyncio.subprocess.STDOUT`, merging the child's stderr into the
  same pipe as its stdout rather than opening a second, undrained one.
  Two distinct risks this closes: a chatty analyzer (Semgrep in particular
  writes progress/warning output to stderr) writing enough to fill an
  undrained ~64KB OS pipe buffer would otherwise block on its own write —
  a classic `asyncio` subprocess deadlock that stalls the call until the
  outer step timeout eventually fires, silently evading the byte-cap
  mechanism above; and an inherited (rather than piped) stderr would let
  attacker-influenced tool output, which can quote raw source/secret text,
  flow unbounded and unredacted straight into Worker container logs,
  undermining the `TextRedactor` guarantee established elsewhere in this
  phase. Merging into one already-capped, already-drained stream closes
  both with no second mechanism to build or maintain — the single
  chunked-read-with-cap loop above now covers everything the process
  writes, on either stream, and the fatal-invocation test case (below)
  that needs error text to assert against reads it from that same merged
  stream. Together, timeout-cancellation and the output-size cap
  are the two paths that can end an analyzer subprocess early, and both
  now converge on the same kill-and-reap mechanism, so there's exactly one
  place this plan's own review (and any future reader) needs to check for
  correct process cleanup, not two divergent ones; for Semgrep specifically (a Click-based CLI taking
  a list of file/directory positional `TARGETS`), it inserts a `--`
  end-of-options separator before every filename argument, so a
  PR-controlled filename beginning with `-` (e.g. a file literally named
  `--config=...`) is always treated as a positional path, never parsed as a
  CLI flag. Gitleaks is invoked against the materialized temp-directory
  path itself (`detect --source <dir> --no-git`), not a list of individual
  per-file arguments, so this specific argument-injection surface doesn't
  apply to it — the path-traversal/symlink guards on what gets written into
  that directory are Gitleaks' relevant mitigation instead.
- **Sub-step enablement**: `StepEnablement` (Phase 2) gains two more
  boolean fields, `static_analysis_gitleaks_enabled` and
  `static_analysis_semgrep_enabled`, alongside the four macro-step fields
  — `pipeline-step-framework.md`'s US-F4 applies at the sub-step level too,
  and the static-analysis macro step's inner registry (above) reads these
  the same way the outer registry reads the macro-step fields, via the same
  `StepEnablement` instance passed down from the composition root.
- Semgrep runs against the vendored, pinned ruleset described in
  Assumptions (not live `--config=auto`); Gitleaks runs with its default
  detection rules plus `--redact`, so a matched secret's actual value never
  appears in Gitleaks' own JSON output — only file/line/rule metadata does
  (no live network dependency either way — Gitleaks has no registry-fetch
  behavior to begin with). This matters beyond normal finding hygiene:
  Phase 7's dry-run mode logs the would-be finding payload at `info`, and
  an unredacted secret value would otherwise land in the platform's own
  structured logs, defeating the point of a secret-scanner finding it.
- **Static-analysis-wide secret redaction, applied to both analyzers.**
  Gitleaks' own `--redact` flag (above) only covers Gitleaks' *own* JSON
  output — Semgrep's match messages routinely quote the matched source
  line verbatim, which can itself contain a live secret/credential
  incidental to the flagged pattern (or a hardcoded-credential rule in the
  pinned ruleset matching on the secret directly). New `findings/redact.py`
  defines a `TextRedactor` `typing.Protocol` (one method,
  `redact(text: str) -> str`), reused by `review/llm_review.py` (Phase 6)
  below. It lives in `findings/redact.py`, not `review/static_analysis.py`
  or `review/llm_review.py`: it has two real step-module consumers and no
  pydantic data model of its own, so under ADR-0006's own rule
  (`docs/adr/0006-protocol-defined-at-data-model.md`, Phase 4) it belongs
  in the bounded-context package it most substantively serves —
  `findings`, since it operates on `Finding`'s free-text fields — rather
  than arbitrarily inside one of its two step-module consumers, which
  would also make one step module import another (forbidden by
  `review/test_step_boundaries.py`, Phase 2). `adapter/analyzers/gitleaks.py`
  implements it — the concrete implementation reuses the same vendored
  Gitleaks binary this phase already vendors (invoked a second time, in
  `--no-git` directory mode against a scratch file holding just the
  candidate finding text, itself with `--redact` set so the raw candidate
  secret never transits through this second invocation's own output
  either, materialized the same path-traversal-safe way this phase's file
  content already is, going through the same `subprocess_runner.py` helper
  as the analyzer sub-steps above — getting the same kill-on-cancellation
  and bounded-chunked-read behavior for free rather than a second
  implementation — and torn down the same way) rather than inventing a
  second secret-detection mechanism. **Scope of the guarantee, stated
  precisely rather than left implicit:** this redacts secrets *detectable
  by the vendored Gitleaks ruleset* — a regex/entropy-based scanner, not an
  exhaustive one. A secret in a format outside that ruleset's coverage
  (e.g. a bespoke internal token shape) can still pass through unredacted
  into a posted PR comment; "redacted" means "matched by Gitleaks' rules,"
  not "no secret of any kind survives." `review/static_analysis.py` calls it
  on every `Finding`'s free-text fields (`message` and any other free-text
  surface the model carries) from **either** analyzer before the step
  returns its findings — not only Semgrep's, applying the same guarantee
  uniformly rather than special-casing by source tool, which keeps the
  call site and its tests simple and gives defense-in-depth over Gitleaks'
  own `--redact` too. A redaction-call failure (subprocess error, timeout,
  unparseable output) is **fatal** for this macro step, the same
  fail-closed reasoning Phase 6 applies to its own reuse of this mechanism
  below — posting an unredacted finding is a materially worse outcome than
  failing the one job affected. **Per-call timeout, not only the outer
  macro-step budget:** without it, a single pathological redaction
  candidate could consume the step's entire remaining budget before the
  coarser outer timeout ever fires, unlike the analyzer sub-steps, which
  are each individually bounded. Each redaction subprocess invocation
  passes its own short timeout into `subprocess_runner.py` (`10` seconds —
  generous for a single-candidate scratch-file scan, the same kind of
  small, short-lived invocation the analyzer sub-step budget already
  assumes at a much larger scale), reusing the exact same
  `asyncio.wait_for`-plus-kill-and-reap mechanism the analyzer sub-steps
  use rather than a second timeout implementation; a redaction call that
  exceeds it is treated the same as any other redaction-call failure —
  fatal for the macro step, per the fail-closed reasoning above — rather
  than silently consuming the rest of the outer budget. **Finding-count
  ceiling on the redaction loop, closing the same unbounded-subprocess-spawn
  risk Phase 6 caps for LLM-sourced findings:** each redaction call spawns
  its own Gitleaks subprocess, so an unbounded number of static-analysis
  findings (a pathological diff that trips a pinned Semgrep rule hundreds
  of times, or a Gitleaks ruleset matching pervasively across many files)
  drives an unbounded number of subprocess spawns before any ceiling
  applies — the exact resource-exhaustion class Phase 6 identifies and
  mitigates for the LLM path ("each one spawns its own Gitleaks
  subprocess, so an unbounded count is an unbounded subprocess-spawn
  multiplier"). `review/static_analysis.py` caps the total number of
  findings (combined across both analyzers) it will individually redact at
  a fixed ceiling (e.g. 50 — higher than Phase 6's 20 since static-analysis
  findings are typically more numerous in a real PR but still bounded);
  findings beyond the ceiling are **dropped entirely, not redacted and not
  returned** — never posted or logged with their raw text, since this is a
  security mechanism and letting an unredacted finding slip through by
  skipping only its redaction call would be worse than dropping the
  finding — logged at `warning` with the count dropped, the same
  capped-and-logged posture as this plan's other ceilings.
  **Independent of `static_analysis_gitleaks_enabled`:** the Gitleaks
  *scanner* sub-step's own enablement flag (an ordinary, expected US-F4
  per-step toggle an operator might flip purely to reduce scanner noise)
  is a different decision from "should secret values be redacted before
  any static-analysis or LLM-sourced finding is logged or posted" (a
  security control unrelated to scanner noise) — coupling them would mean
  an operator disabling the scanner for an unrelated reason also silently
  disables redaction of any secret the LLM (Phase 6) echoes back verbatim
  into its own finding prose, which then gets posted as a real PR comment
  on the customer's repo. `WorkerSettings` gains an independent
  `secret_redaction_enabled: bool` field (default `True`), separate from
  `static_analysis_gitleaks_enabled`. Redaction invokes the vendored
  Gitleaks binary directly, as its own concern, regardless of
  `static_analysis_gitleaks_enabled`'s value — the binary is vendored
  unconditionally (per the Assumptions section's packaging bullet), so
  it's available to the redaction path whether or not the scanner sub-step
  itself is enabled. The composition root (`apps/worker/main.py`) wires
  this step's `TextRedactor` dependency conditionally on
  `secret_redaction_enabled` alone: `True` → the real Gitleaks-backed
  implementation; `False` → a no-op passthrough plus one `warning`-level
  log line at Worker startup — "secret redaction is disabled" — so the
  reduced protection is visible, not silent, and an operator has to make
  that choice deliberately rather than as a side effect of an unrelated
  scanner toggle. Phase 6 below reuses this exact `TextRedactor` Protocol
  and the same `secret_redaction_enabled` flag for LLM-sourced findings
  rather than defining its own or inheriting Phase 5's Gitleaks-scanner
  flag.
- **Non-root container user, added here since this is the first phase
  running real subprocesses against untrusted input — see the Security
  review's dedicated "No process-level privilege separation" bullet for the
  full threat reasoning:** the `Dockerfile`'s `runtime` stage adds a
  dedicated, minimally-privileged OS user (e.g. `useradd --system --no-create-home
  worker` plus a matching `USER worker` directive after the stage's
  `COPY`s, so file ownership is set before the privilege drop) and sets it
  as the default user for both entrypoints this image serves — a
  compromised Gitleaks/Semgrep/redaction subprocess no longer runs as
  `root` inside the container, closing the most direct escalation path at
  negligible build/runtime cost while true sandboxing stays deferred to
  Phase 8 per that section's own reasoning.
- **Resource limits** (closing the gap between this pipeline's per-step
  timeout and actual container enforcement): `docker-compose.yml`'s
  `worker` service gains explicit bounds (`mem_limit: 2g`, `cpus: "2"` —
  concrete starting figures sized to run Gitleaks/Semgrep plus the Worker
  process comfortably on a typical single-node Compose deployment, tunable
  per-deployment, not a value the platform hardcodes), and
  `charts/review/values.yaml`'s `worker.resources` gains a matching
  non-empty default (`requests: {cpu: 500m, memory: 512Mi}`,
  `limits: {cpu: "2", memory: 2Gi}` — currently `{}`, opt-in only), and
  `charts/review/test-render.sh` (Phase 0's existing Helm-render check gains
  a new assertion on the rendered Worker Deployment's
  `resources.requests`/`resources.limits` values matching these defaults —
  a gap an earlier draft of this bullet left unaddressed, since this is the
  first phase to give `worker.resources` a real, non-`{}` value for that
  script to have anything meaningful to assert on) — a
  pathological diff or a misbehaving analyzer subprocess is bounded by
  both the per-step timeout (already in Phase 2) and a hard container
  ceiling, not the timeout alone. These figures already account for the
  Gitleaks and Semgrep sub-steps each materializing their own copy of the
  same pre-filtered `Diff` into a separate temp directory and running
  concurrently (per Phase 4's adapter-owned materialization) — a
  deliberate tradeoff for per-analyzer isolation (one analyzer's
  materialization bug can't corrupt another's input) over the smaller,
  single-shared-directory footprint a macro-step-owned temp directory
  would have had; for Phase 1's PR-sized diffs this roughly doubles
  peak temp-file disk usage, not memory, and stays well inside the
  `mem_limit`/`cpus` figures above. Phase 4's aggregate-materialized-bytes
  ceiling (see that phase's own "Aggregate-bytes ceiling" bullet) is sized
  with this doubling already accounted for, so the two per-analyzer
  temp-directory copies this bullet describes stay within the disk budget
  that ceiling assumes. **Redaction subprocess cost, considered rather
  than left unaccounted-for in this sizing:** the finding-count-capped
  redaction pass above (up to 50 static + 20 LLM findings) adds up to 70
  additional short-lived Gitleaks invocations per job, run sequentially
  within the redaction loop, not concurrently with each other or with the
  main analyzer scans (which have already completed by the time redaction
  runs) — judged to fit within the headroom already present in the
  `cpus: "2"` figure rather than needing its own separate budget line, but
  named explicitly here rather than left as a silent gap in the sizing
  rationale.
- `Dockerfile` (`runtime` and `dev` stages): add `semgrep` to
  `pyproject.toml`'s main dependencies (picked up automatically by the
  existing `uv sync` layers); add the Gitleaks binary vendoring step
  described in Assumptions.
- `.github/workflows/ci.yml`'s `integration-test` job: add a step
  installing/verifying the pinned Gitleaks binary on the runner (Semgrep
  is already covered by the job's existing `uv sync --locked`, since it's
  now a `pyproject.toml` dependency) — needed because this job runs
  `pytest -m integration` directly on the runner, not inside the built
  Docker image.

**Tests:** Integration (per `testing.md`'s explicit "static-analyzer
adapter against actual binaries on fixtures" convention, and the PRD's own
Testing table). **Marker note, per Phase 2's own flag:** these cases join
Phase 2's existing Unit-tier fake-sub-step cases in the same
`review/test_static_analysis_step.py` file, so `@pytest.mark.integration`
is applied per-test to this phase's additions specifically, not via a
module-level `pytestmark` — the same per-test-not-per-module rule applies
to `adapter/analyzers/test_subprocess_runner.py` below, which similarly
gains Integration-tier real-binary cases (kill-and-reap, bounded-read) in
the same file as Phase 4's own Unit-tier path-guard tests. US4 (both analyzers run, findings normalized) and US9 (one
analyzer raising doesn't prevent the other's findings + the pipeline
completing) in `review/test_static_analysis_step.py`, against small
committed fixture files with a known planted secret (for Gitleaks) and a
known planted pattern (for Semgrep's pinned ruleset) — deterministic
because both the ruleset and the fixtures are pinned, per the Assumptions
correction above. A third case covers `pipeline-step-framework.md`'s US-F5
timeout branch (not just the raises branch already covered by US9):
a fixture analyzer command wrapped to sleep past a deliberately short
per-step timeout is isolated the same way a raising analyzer is, and the
other analyzer's findings still complete the review — this is the direct
test of the mechanism the Security review names as the mitigation for
resource exhaustion via a pathological analyzer, so it gets its own
asserted case rather than living only as an unverified claim in that
section. A fourth case covers a distinct failure mode from
process-raises/times-out: a fixture analyzer command that exits `0` but
emits malformed/unparseable JSON (a plausible real-world case — tool-version
drift against the pinned ruleset, truncated output) is treated as a typed,
isolated sub-step failure, the same isolation the raising/timeout cases
above already prove, not an unhandled parse exception that could escape
the step's own error handling — the test also asserts this case's failure
log fires at `error`, per this step's own Observability convention above. **Two further cases, proving the kill-and-reap and bounded-read
mechanisms themselves rather than only the pipeline-level isolation they
enable — a gap this plan's own review caught, since the timeout-isolation
case above proves the step is isolated without proving the underlying OS
process is actually terminated rather than orphaned:** in
`adapter/analyzers/test_subprocess_runner.py`, a fixture command that
would otherwise keep running past the test's own lifetime if not killed
(e.g. a long sleep) is wrapped with a deliberately short per-call timeout,
and the test asserts the child process is confirmed terminated
(`process.returncode` is set, not `None`) once the timeout fires and
`subprocess_runner.py`'s call returns — not merely that the Python-side
call raised or returned. A second, independent case asserts the bounded
chunked-read byte cap: a fixture command that emits output past the
configured cap is killed and reaped the same way, and the call fails with
the typed "analyzer output exceeded the size cap" error rather than
running to completion or the test process's own memory growing
unboundedly while reading it. Fixture files live under `review/testdata/` (a non-package
directory, excluded from `pyproject.toml`'s `testpaths`) and the planted
secret is an obviously-fake, clearly-marked placeholder value kept out of
any path a future Gitleaks-on-this-repo's-own-CI pre-commit hook would
scan — noted explicitly so this phase's own fixture doesn't become a false
positive if Gitleaks is ever added to this repo's own tooling later. A
dedicated case in `adapter/analyzers/test_subprocess_runner.py` asserts a
committed fixture filename beginning with `-` is passed to the tool as a
positional argument, not interpreted as a flag. A dedicated case in
`adapter/analyzers/test_gitleaks.py` asserts the concrete `TextRedactor`
implementation actually redacts a known-fake secret pattern via a real
subprocess call, and a companion negative case (the Gitleaks binary given
a deliberately-invalid invocation) asserts the fatal-failure behavior
above, not a silent pass-through. A case in
`review/test_static_analysis_step.py` asserts a Semgrep finding whose
message contains a known-fake secret pattern is redacted before the step
returns, proving the gap this bullet closes (not only Gitleaks' own
`--redact`-covered output) with a fake `TextRedactor` isolating the
call-every-finding behavior from the concrete implementation's own
redaction-correctness test above. A companion case in the same file
asserts a fake `TextRedactor` that raises produces the fatal
`PipelineFailedError` path for `review/static_analysis.py` — mirroring
Phase 6's equivalent test for its own call site below, so this macro
step's own redaction-failure propagation is proven directly rather than
left as an unverified extrapolation from Phase 6's test of the same
mechanism at a different call site. Boundary cases for the redaction
loop's finding-count ceiling (exactly at 50, one over, comfortably under)
assert findings beyond the cap are dropped — not redacted, not returned —
and logged at `warning` with the count dropped, mirroring the same
boundary-testing rigor Phase 6 applies to its own parallel 20-finding cap,
so this cap's silent-drop, product-visible behavior isn't left unverified
the way this plan's own review caught a prior draft leaving it. **A gap
this plan's own review caught: the non-root `USER worker` Dockerfile
directive above had no corresponding check anywhere, inconsistent with
this same phase's resource-limit changes, which do get a `test-render.sh`
assertion.** A new E2E-tier case in `tests/e2e/test_compose_stack.py`,
alongside the container-state assertions Phase 3 already established
there, brings up the Worker service via the same Compose stack and asserts
its running process UID is non-zero (`docker compose exec worker id -u`,
matching how that file already shells into the running stack for its
existing checks) — proving the built image actually runs as non-root, not
only that the Dockerfile source says so; a regression here (e.g. a future
edit accidentally dropping the `USER` line) would otherwise go undetected
until an actual incident. Two further
cases, in
`apps/worker/test_main.py` (composition-root wiring, per this plan's
existing convention for testing conditional dependency construction),
prove the decoupling above rather than the coupled behavior an earlier
draft would have tested: (1) with `secret_redaction_enabled=False`, the
`TextRedactor` wired into both this step's and Phase 6's call sites is the
no-op passthrough, not the concrete Gitleaks-backed implementation, and
the one-time startup `warning` log fires exactly once at Worker boot, not
per-job; (2) with `static_analysis_gitleaks_enabled=False` and
`secret_redaction_enabled` left at its default `True`, the wired
`TextRedactor` is still the real, concrete Gitleaks-backed implementation —
proving the scanner-disable flag no longer silently disables redaction,
the exact gap this plan's own review caught and corrected above.

**Observability & rollback:** Each analyzer sub-step logs its own
start/completion/failure/finding-count (extending Phase 2's per-step
logging with `analyzer_name`) — a sub-step failure, including a
malformed/unparseable-output case, logs at `error` (a confirmed defect in
the tool invocation, not merely absorbed noise), even though it's isolated
and doesn't fail the macro step itself; the redaction pass gets its own explicit
logging, not folded silently into the step's general logging — success
logs at `info` with a per-job redaction-attempt and redaction-hit count,
failure logs at `error` with the typed exception before the step fails
fatally, and the Gitleaks-disabled passthrough case logs its one-time
`warning` at Worker startup, not per-job (Phase 6 reuses this exact
logging shape for its own call into the same mechanism, rather than
inventing a second one). **The kill-on-timeout path logs distinctly from
an ordinary tool failure, not as the same generic message:**
`subprocess_runner.py`'s `process.kill()`-and-reap branch logs at `error`
with a dedicated reason string (e.g. `"analyzer step killed after
exceeding its timeout"`, distinct from an organic non-zero-exit/crash
failure's own message) before the timeout propagates as the step's fatal
exception, so an operator reading logs can tell "we killed a hung
process" apart from "the tool crashed on its own" without inspecting
exception types by hand — the output-size-cap kill path already gets its
own distinct "analyzer output exceeded the size cap" message above; this
gives the timeout-kill path the same treatment rather than leaving it
folded into a single undifferentiated failure log. A temp-directory teardown failure (the
`finally` block's own cleanup raising, e.g. a permissions error) is caught
and logged at `warning` with the directory path rather than left to fail
silently — a leaked per-job temp directory should be visible in logs, not
only discoverable by an operator noticing disk usage climbing. Rollback:
disabling a misbehaving analyzer is a bootstrap-config change (Phase 2's
per-step on/off), not a code rollback — the operational lever this whole
framework exists to provide. The new container resource limits have no
application-level signal when they're the actual cause of a failure (an
OOM-killed analyzer subprocess or container looks the same as any other
crash from inside the Worker's own logs) — Docker/Kubernetes' own
event log is the only source of truth for that distinction in this phase;
surfacing it at the application level (e.g. detecting a `SIGKILL`
exit code and logging it distinctly from an ordinary crash) is left to
Phase 8, not solved here.

**Acceptance:** the Integration tests above pass in CI, including on the
CI runner's newly-installed Gitleaks binary (not just locally); a
deliberately-broken fixture (analyzer raises) still yields a completed
review using the other analyzer's findings, proving US9 end-to-end, not
just at the unit-fake level Phase 2 already proved; the timeout-branch case
above passes, proving US-F5's full Given clause (raises **or** exceeds its
timeout), not only the raises half; `mypy --strict` confirms
`review/static_analysis.py` imports nothing from `adapter/analyzers`
directly (only the `StaticAnalyzer` Protocol) and performs no filesystem
I/O of its own, the same Protocol-boundary and zero-infra-imports proof
every other phase's Acceptance section requires for its own domain/adapter
pair.

## Phase 6 — LLM-review step

**Why here:** depends on Phase 2 (the framework), Phase 4 (consumes the
`Diff` `ingestion/diff.py` already fetched — this phase no longer fetches
its own, see Phase 4's correction note), and Phase 5 (consumes
static-analysis findings as context, per the PRD's US5) but not on Phase 7
— its own output (structured findings) is Phase 7's *input*, not the other
way around.

- New `review/llm_review.py`: the macro `Step` implementation. Builds a
  `CompletionRequest` (`llmgateway/models.py`, unchanged) from the bare diff
  (Phase 4's `Diff`, passed through `StepContext`/pipeline wiring — not
  re-fetched here) and Phase 5's static-analysis findings, calls the
  existing `llmgateway.client.LLMClient` Protocol
  (`adapter/llm/litellm_client.LiteLLMClient`, unchanged — this phase is
  the first real *caller* of that Protocol outside its own startup check),
  and parses the response against a fixed structured-findings schema
  (`findings/models.py`'s `Finding`, extended from Phase 5 with a
  `source: Literal["static", "llm"]` discriminator and a `tool`/`rule_id`
  field that's `None` for LLM-sourced findings). The diff is capped at a
  fixed byte/file-count ceiling before being included in the prompt (a new
  `WorkerSettings.llm_max_diff_bytes` field, default `200_000` — roughly a
  few hundred changed lines, generous for a genuine review-sized PR while
  bounding worst-case token cost — matching every other numeric
  bootstrap-config field's pattern) — an oversized PR truncates rather than
  sending an unbounded prompt, since diff size is fully PR-author-controlled
  and directly drives LLM token cost per the Security review. A truncation
  logs at `warning` with the original and truncated byte counts, the same
  operator-visible-drop posture as every other cap this plan adds
  (materialization/aggregate-bytes ceilings, the redaction finding-count
  caps, the posted-findings ceiling) — an earlier draft left this one
  silent, inconsistent with that pattern.
- **LLM-output secret redaction, reusing Phase 5's mechanism rather than
  defining a second one** — see Phase 5's own bullet for the Protocol
  definition, concrete implementation, and disable-flag wiring. Gitleaks'
  own `--redact` flag (Phase 5) only affects Gitleaks' own tool output —
  it says nothing about the LLM, which receives the same unredacted diff
  as its prompt input (Phase 5's static findings are *context*, not a
  filter on what the LLM itself sees) and can echo a secret-shaped
  substring back verbatim in its own finding prose (`source: "llm"`
  findings). Uncaught, that's worse than the dry-run-log exposure Phase 5
  closes: an LLM-sourced finding quoting a live secret could be posted as
  a real PR review comment on the customer's repo, not just logged.
  `review/llm_review.py` calls the same `TextRedactor` `typing.Protocol`
  Phase 5 defines in `findings/redact.py` — one Protocol, one
  composition-root-constructed instance (`apps/worker/main.py`), passed by
  constructor injection to **both** steps, not redefined or re-wired here.
  The instance both steps receive is gated on the independent
  `secret_redaction_enabled` flag (Phase 5), not on
  `static_analysis_gitleaks_enabled` — so toggling the Gitleaks scanner
  sub-step off has no effect on redaction for either
  step's findings, LLM-sourced or static-analysis-sourced, with no separate
  flag-interaction logic needed in this phase beyond consuming the same
  shared instance Phase 5 already wires. `review/llm_review.py`
  calls it on every `source: "llm"` `Finding`'s free-text fields before
  they're added to the merge input, itself doing no subprocess or
  filesystem I/O — same boundary pattern used everywhere else in this
  plan. Any match redacts that portion of the message before the finding
  exists anywhere else in the pipeline, so dry-run logging, real posting,
  and any future consumer all inherit the same guarantee Phase 5
  established for static findings.
- **Redactor-call failure behavior for this phase's own call site,
  specified rather than left implicit:** if the redaction call itself
  errors, times out, or produces unparseable output, `review/llm_review.py`
  treats it as a **fatal** failure of the llm-review macro step (the same
  `PipelineFailedError` path as a malformed LLM response, and the same
  fail-closed reasoning Phase 5 applies to its own call site) — never a
  silent pass-through of unredacted text. Logged at `error` with the typed
  exception, `job_id`, and a redaction-attempt count, extending this
  step's existing logging rather than a new setup. The number of
  `source: "llm"` findings redaction iterates over is capped
  at a small fixed ceiling (e.g. 20 — a real code review has no legitimate
  reason to produce hundreds of individual findings from one LLM
  completion), reusing the same reasoning as the diff-size cap above: an
  unbounded finding count is PR/LLM-response-controlled input, and each
  one spawns its own Gitleaks subprocess, so an unbounded count is an
  unbounded subprocess-spawn multiplier bounded only by the step's overall
  timeout rather than an explicit limit. Findings beyond the cap are
  dropped (not redacted, not posted) and logged at `warning` with the
  count truncated — a review with an implausibly large finding count is
  more likely a malformed/adversarial LLM response than a real one.

**Tests:** Integration (per `testing.md`'s "LLM gateway against a recorded
LiteLLM cassette" convention, and the PRD's Testing table): US5's
`Given/When/Then` — one completion call made with diff + static findings as
context, response parsed against the structured schema — in
`review/test_llm_review_step.py`, against a recorded cassette (not a live
LM Studio call in CI, per `testing.md`'s LLM-determinism rule). Two negative
cases: (1) a malformed/unparseable LLM response asserts a typed failure,
not an unhandled exception, and that this failure logs at `error` per
Phase 2's macro-step-fatal-failure default (this is a job-failing case, not
an isolated sub-step case); (2) an LLM Gateway timeout (the PRD's own US8
scenario, `docs/specs/phase-1-review-pipeline.md`) asserts the same typed
failure path via a cassette recorded with a timeout/connection-error
response, and — matching case (1) — that this also logs at `error` per the
same Phase 2 default; both feed directly into Phase 7/US8's fatal-failure
path.
The concrete `TextRedactor` implementation's own redaction-correctness and
fatal-failure-behavior tests live in Phase 5's `adapter/analyzers/test_gitleaks.py`
(the mechanism is defined and tested where it's first built, per Phase 5's
own bullet) — not duplicated here, since this phase only adds a second
call site against the same already-tested implementation. Unit (per
`testing.md`'s "new domain logic still needs unit tests" rule, both
pure/no-I/O against a fake `LLMClient` and a fake `TextRedactor`):
diff-truncation boundary cases (`review/test_llm_review_step.py`) — a diff
exactly at `llm_max_diff_bytes`, one byte over, and comfortably under,
asserting the prompt is truncated only in the over case; a dedicated case
asserting `review/llm_review.py` calls the fake `TextRedactor` on every
`source: "llm"` finding's message before merge (proving the
call-every-LLM-finding behavior in isolation, distinct from Phase 5's
Integration test, which proves the concrete Gitleaks-backed
implementation's actual redaction behavior); a case asserting a fake
`TextRedactor` that raises produces the fatal `PipelineFailedError` path,
not a swallowed exception; boundary cases for the redaction finding-count
cap (below) — exactly at the cap, one over, and comfortably under —
asserting findings beyond the cap are dropped (not redacted, not passed to
merge) and logged at `warning` with the truncated count, the same
boundary-testing rigor already applied to `llm_max_diff_bytes` above, so
this cap's silent-drop, product-visible behavior isn't left unverified the
way this plan's own review caught it as being in an earlier draft.

**Observability & rollback:** Extends `adapter/llm/litellm_client.py`'s
existing request/latency/model logging (already present from Phase 0) with
this step's own `job_id`/`step_name` context. **Confirmed, not assumed:**
that existing logging (`litellm_client.py`'s `logger.info`/`_log_failure`
calls) logs only metadata — endpoint, model, timeout, latency, error class
— never the request/response body, so the raw diff sent as prompt content
is not written to Worker logs pre-redaction by this extension; this phase
adds no full-body logging either, so `TextRedactor`'s coverage of
`Finding` objects (Phase 5, reused here) remains the only place a
secret-shaped substring from the diff could otherwise surface unredacted,
and it's covered there. This step's own redaction
call site reuses Phase 5's logging shape unchanged (per-job
redaction-attempt/hit count at `info`, typed exception at `error` before
failing fatally) — no separate logging setup here, and the
Gitleaks-disabled passthrough case's one-time startup `warning` (Phase 5)
already covers this step's call site too, since both share the one
composition-root-wired `TextRedactor` instance. Rollback: disabling this
step via Phase 2's per-step
config falls back to static-analysis-only output, a legitimate operator
choice the framework already supports.

**Acceptance:** the Integration tests above pass against the recorded
cassette; `mypy --strict` confirms `review/llm_review.py` imports nothing
from `adapter/llm` or `adapter/analyzers` directly (only
`llmgateway.client.LLMClient` and Phase 5's `TextRedactor` Protocol,
reused rather than redefined),
preserving the Protocol boundary Phase 0 already established and matching
it for the new redaction dependency.

## Phase 7 — Findings-merge-and-output step + GitHub Adapter posting client

**Why here:** depends on Phase 2 (framework), Phase 5 (static findings),
and Phase 6 (LLM findings) — needs both output shapes finalized, per the
PRD's own Unit G dependency note. This is also the phase that closes the
loop and satisfies the roadmap's Phase 1 exit criteria, so it carries the
plan's E2E test, mirroring the role Phase 0's plan gave its own Phase 5
("this is the integration point"). **`adapter/github/auth.py` (the
installation-token provider), its `pyjwt[crypto]` dependency, and the
`github_app_id`/`github_app_private_key` bootstrap config are all already
built, in Phase 4, not here — see that phase's "Why here" correction.**
An earlier draft of this plan built `auth.py` in this phase while having
Phase 4's diff-fetch depend on and test against it, a self-contradictory
forward dependency this plan's own review caught (Phase 4 cannot function
or be tested without a token provider that, as originally drafted, didn't
exist for three more phases). This phase's `posting.py` (below) simply
constructor-injects the same `adapter/github/auth.py` client Phase 4
already built and the composition root already wires — no new auth code,
no new config, of its own.

- New `adapter/github/posting.py`: two methods — one `POST`ing a single PR
  review (`/repos/{owner}/{repo}/pulls/{pr}/reviews`) carrying every inline
  comment in one call (the batching decision from both PRDs' NFR sections),
  one `POST`/`PATCH`ing a check-run (`/repos/{owner}/{repo}/check-runs`)
  with conclusion always `"neutral"` per the PRDs' confirmed decision. Both
  the comment bodies and the check-run's summary text (which embeds a
  finding-count string built from the same PR/LLM-derived data) are built
  via a small Markdown-escaping helper — no raw PR-derived or LLM-derived
  text passed through unescaped on *either* surface, not just the PR
  review comments; a prior draft of this bullet only stated the comment
  body case, leaving the check-run summary text as an unstated second
  place the same GFM-injection vector could reopen. New
  `findings/markdown.py` (not `adapter/github/posting.py` itself): the
  helper is pure text transformation with no I/O, so it lives in the
  `findings` domain package alongside `findings/merge.py` — consistent
  with `testing.md`'s Unit tier and avoiding the ambiguity of a pure-logic
  helper embedded inside `adapter/github`, which `testing.md` otherwise
  assigns wholesale to the Integration tier. **Tension with
  `findings/__init__.py`'s own docstring, flagged rather than silently
  overridden:** that docstring currently scopes the package as "domain
  model and merge/dedupe logic for review findings, independent of how
  they are posted or stored" — a Markdown-escaping helper exists
  specifically *for* the posting surface, which that wording disclaims.
  This plan keeps the placement (the testing-tier argument above is the
  stronger constraint, and `findings/merge.py` already establishes the
  package as where findings-adjacent pure logic lives) but flags that
  `findings/__init__.py`'s docstring should drop the "independent of how
  they are posted" clause as part of this phase's own PR, the same way
  Phase 0's `review/__init__.py` docstring precedent is treated elsewhere
  in this plan's Assumptions — not left as a docstring quietly describing
  a narrower scope than the package actually has. `adapter/github/posting.py`
  imports and calls it for both surfaces; genuinely new, not a reuse of
  `apps/webhook_api/manifest_routes.py`'s `html.escape` call, which
  escapes a different domain (HTML entities for a manifest JSON blob, not
  GitHub-Flavored-Markdown special characters for a PR comment/check-run).
  The helper neutralizes GFM link/image/`@mention` syntax specifically,
  not only code-fence-breaking characters — LLM output is untrusted,
  prompt-injectable content per this plan's own Security review, and an
  unescaped `[text](url)`/`![img](url)`/`@user` sequence posted from the
  App's bot identity is a phishing/notification-spam vector, not just a
  rendering-glitch risk. **Raw inline HTML, not only GFM markdown syntax:**
  GitHub's GFM renderer also permits raw inline HTML tags in PR review
  comments and check-run summaries, a second injection surface distinct
  from GFM link/image/mention syntax — a Semgrep match message quoting a
  source line verbatim, or an LLM-sourced finding reflecting attacker
  content, can carry an untrusted `<img src="...">`/`<a href="...">` tag
  that would otherwise render as a live remote-content fetch (a
  tracking-pixel/beacon and phishing vector) to every subsequent viewer of
  the PR. The helper therefore also escapes literal `<`/`>` characters, so
  any embedded HTML tag renders as inert text rather than being
  interpreted by GitHub's renderer; `findings/test_markdown.py` (below)
  covers this with a case embedding an `<img src=...>`/`<a href=...>` tag
  in finding text and asserting it renders as literal escaped text, not a
  live tag. **Known, accepted coupling, not addressed
  further here:** `findings/markdown.py` encodes GitHub-specific GFM
  rendering rules inside the `findings` domain package, which
  `architecture.md`'s own worked example for the "new adapter = zero
  domain changes" boundary rule (a hypothetical GitLab adapter alongside
  the GitHub one) would need to touch for a different git host's own
  Markdown dialect. A second git-host adapter is not in this plan's scope
  (see "Out of scope"), so this coupling is accepted for Phase 1 rather
  than solved with a git-host-agnostic escaping abstraction this plan has
  no second implementation to justify, per `architecture.md`'s own
  "no interface without a second real implementation already in view"
  rule. **Second, related acceptance:** `adapter/github/posting.py` calls
  this helper as a plain function, not through a Protocol, in tension with
  `architecture.md`'s "adapters depend inward on domain-defined Protocols
  only" framing read literally; accepted for the same reason — a pure,
  stateless utility with no swappable-implementation case in view has
  nothing a Protocol would abstract over, so introducing one here would be
  exactly the kind of interface-without-a-second-implementation
  `architecture.md` itself warns against.
- New `findings/merge.py`: pure dedupe/combine logic (no I/O) — same
  file+line+message-similarity findings from static and LLM sources
  collapse to one; `findings/__init__.py`'s docstring already designates
  this package for exactly this. **Posted-findings ceiling, specified
  rather than left implicit:** GitHub's PR-review API has its own
  practical limits on comment count/payload size per review; an
  otherwise-successful review with an implausibly large merged-finding
  count (a pathological diff or a redaction-cap-adjacent malformed LLM
  response) could fail the posting call outright with no findings
  delivered at all. `findings/merge.py` caps the merged output at a fixed
  ceiling (e.g. 100) after dedupe; findings beyond it are dropped and
  logged at `warning` with the count truncated, the same capped-and-logged
  posture as this plan's other ceilings, so a large-but-real diff still
  gets a posted review instead of an outright posting failure.
- New `review/output.py`: the macro `Step` wrapping `findings/merge.py` and
  a new `FindingsPublisher` `typing.Protocol` (defined here, the consumer,
  per `architecture.md` — `post_review(...)` for the batched PR review,
  `post_check_run(...)` for the check-run, plus `post_failure_check_run(...)`
  for the case below), implemented by `adapter/github/posting.py`.
  `review/output.py` imports only the Protocol, never
  `adapter/github/posting.py` directly — the concrete client is wired in
  at the composition root (`apps/worker/main.py`), mirroring exactly how
  Phase 6 binds `llmgateway.client.LLMClient` rather than `adapter/llm`
  directly. Implements the PRD's US7 (always posts a check-run, even with
  zero findings) and US8, **scoped correctly — every fatal macro-step
  failure, not only llm-review's, and a genuine reversal of
  `pipeline-step-framework.md`'s own US-F6 wording, flagged as such rather
  than silently shipped as a compatible extension:** a prior draft of this
  bullet described US8 as "a fatal upstream failure — from Phase 6's
  llm-review or this step itself," which this plan's own review caught as
  leaving a real gap — Phase 4's pre-filter/diff-fetch step is *also* a
  macro step whose failure is fatal (Phase 4's own Observability &
  rollback section already says so), and under the framework's normal
  dependency-satisfaction rule (Phase 2: a step only runs once its
  declared dependencies succeed), `review/output.py` never runs at all
  when an *earlier* step in the chain fails — meaning the job flips to
  `FAILED` in Postgres with **zero GitHub-visible signal**: no check-run,
  nothing on the PR. A reviewer could easily misread that as "nothing to
  flag" rather than "review never ran," and an attacker able to reliably
  trigger a pre-filter/diff-fetch failure gets a PR that silently never
  receives a review. **This fix once contradicted
  `pipeline-step-framework.md`'s own US-F6 Given/When/Then** ("the job is
  marked FAILED and no downstream step, including posting to GitHub,
  executes") — this plan's review caught that conflict, and rather than
  leave it flagged-but-unresolved across further iterations the way the
  rate-limiter reversal is (a genuine product-policy call this plan
  couldn't settle unilaterally), this specific conflict was a small, purely
  factual wording gap this plan's own fix pass corrected directly:
  `pipeline-step-framework.md`'s US-F6 now states the terminal-step
  exception explicitly (a *normal* downstream step, one that needs its
  dependency's actual output, must never run after that dependency fails;
  `review/output.py`'s *failure path* needs no upstream step's output —
  only the job's PR identity — so it was never really "a downstream step
  running after its dependency failed" in the sense US-F6's original
  wording was written to prevent, and the spec now says so). Mechanism, and where it lives (a
  correction from an earlier draft that hardcoded this in
  `apps/worker/main.py`, which this plan's own review flagged as
  duplicating orchestration ownership the framework exists to hold in one
  place): `review/pipeline.py` (Phase 2) is extended with one designated
  "terminal" step role — the registered findings-output step, if any, is
  invoked exactly once per job regardless of outcome, either with every
  dependency's successful output (the happy path US7 already covers) or
  with a new, concrete `StepFailureInfo` pydantic model (`failed_step_name:
  str`, `error: str`) built from whichever step's `PipelineFailedError`
  propagated (this fix), decided by the registry itself at the point that
  exception propagates — not by `apps/worker/main.py` special-casing "call
  `review/output.py`" in the dispatch loop. **Reconciled with Phase 2's
  `Step` Protocol invariant, not a silent exception to it:** `review/output.py`'s
  `run(...)` signature takes `input: FindingsOutputInput | StepFailureInfo`
  — still one step-specific pydantic model per call, per that invariant's
  substance, just a union of two shapes for the one step in this framework
  that's ever invoked on both a success and a failure path; every other
  step's `input` stays the single concrete model it always was. The dispatch loop stays as
  thin as every other phase's composition-root code: it still just calls
  the pipeline runner and reacts to its outcome. **Honors
  `context.dry_run` like every other GitHub-writing call this plan
  specifies** — the failure path calls the same `context.dry_run` check
  `review/output.py`'s normal path already makes, logging the would-be
  failure check-run instead of posting it when dry-run is set; unaddressed,
  this would have made a live GitHub write happen during dry-run
  validation specifically when a step fails, the scenario an operator
  validating a fresh deployment is likeliest to hit. **Residual gap,
  acknowledged rather than solved here:** if the fatal failure is itself a
  GitHub-API/auth failure (e.g. `adapter/github/auth.py`'s token exchange
  failing), posting the failure-conclusion check-run needs that same
  broken auth path and will itself fail — reintroducing a narrower version
  of the "zero GitHub-visible signal" gap this fix closes, under
  conditions (a GitHub outage or App suspension) where nothing this
  platform does can post anything. Logged distinctly at `error` as a
  double-failure (original cause plus the failed failure-post), not folded
  into Phase 3's generic "bug in the dispatch loop" log line, so it's
  diagnosable as an external-dependency problem rather than a platform
  defect — a real, narrow residual gap left for Phase 8, not silently
  assumed solved. **Rollback:** this mechanism lives entirely in
  `review/pipeline.py` and `review/output.py`, both redeployed together
  with every other Worker-image change per this plan's existing rollback
  pattern (redeploy the prior image) — no separate kill-switch beyond the
  existing per-step `StepEnablement` disable for the output step itself
  and `REVIEW_WORKER_DRY_RUN`. Across all of this, the net behavior is:
  posts a failure-conclusion check-run and zero inline comments — never a
  partial/garbled post, and never a silent one either, regardless of which
  step failed. **Happy-path ordering and partial-failure behavior between
  `post_review(...)` and `post_check_run(...)`, specified rather than left
  as an unstated gap this plan's own review caught — the "never a
  partial/garbled post" guarantee above only ever addressed the
  upstream-failure path (US8); it did not, until this correction, cover the
  two-call happy-path sequence itself:** `review/output.py` calls
  `post_review(...)` first (the higher-value artifact — the inline findings
  a reviewer actually reads) and `post_check_run(...)` second. If the first
  call succeeds and the second then fails, the job is still marked `FAILED`
  (per Phase 3), but this is a materially different case from an ordinary
  fatal step failure: real, customer-visible output (the PR review) is
  already permanently live on the customer's repo, with no check-run to
  accompany it. This is logged at `error`, not the `warning` a routine
  transient GitHub failure gets — a confirmed partial-post defect, not a
  routine retryable condition — with the PR number, which call succeeded,
  and which call failed and why, giving an operator enough to manually
  reconcile (e.g. post the missing check-run by hand). Full transactional
  compensation (deleting or editing the already-posted review to keep the
  two artifacts atomic) is explicitly out of scope for this phase as
  unnecessary complexity for a walking skeleton — GitHub's API has no
  native multi-resource transaction primitive to lean on here, and building
  one would be exactly the kind of speculative machinery this plan avoids
  elsewhere — but the gap must be loud, not silent, which the `error`-level
  log with full reconciliation detail ensures.
- `REVIEW_WORKER_DRY_RUN` bootstrap flag (`apps/worker/settings.py`),
  read once at Worker startup and set on Phase 2's `StepContext` for every
  claimed job — not read directly by `review/output.py` from settings. When
  `context.dry_run` is set, `review/output.py` runs its own merge logic and
  logs the would-be comment/check-run payload at `info` instead of calling
  `adapter/github/posting.py`. Threading it through `context` rather than
  having `review/output.py` read the flag itself is deliberate: any future
  side-effecting step checks the same `context.dry_run` field, so
  extending dry-run coverage to a later step (e.g. a Phase 4 memory write)
  needs no new config field, only that step reading a parameter it already
  receives. This logged payload goes through the same redaction path as a
  real post, not a separate one — via two mechanisms together: Phase 5's
  `TextRedactor` pass (applied to every `Finding` either analyzer produces,
  before this step ever sees them — the fix that closes the gap where
  Gitleaks' own `--redact` flag alone covered only Gitleaks' *own* tool
  output, not Semgrep's, per Phase 5's own bullet) redacts secrets
  detectable by the vendored Gitleaks ruleset out of every static-analysis
  `Finding`, and Phase 6's reuse of that same `TextRedactor` does the same
  for LLM-sourced findings — so dry-run logging doesn't need its own
  separate redaction pass; the same finding objects that would be posted to
  GitHub are what get logged, run through the same ruleset-bounded
  redaction pass either way (see Phase 5's own bullet for what that ruleset
  does and doesn't catch).
**Tests:** Unit (per `testing.md`'s "new domain logic a story touches still
needs unit tests too" rule — both are pure, no-I/O domain logic, not
covered by exercising them incidentally inside a cassette-recorded
Integration test): `findings/test_merge.py` — the file+line+message-similarity
dedupe/combine logic, with cases for a clear duplicate collapsing to one,
two genuinely distinct findings on the same line staying separate, an
empty-input case, and, matching the boundary rigor every other ceiling in
this plan gets, a pair of cases for the posted-findings ceiling: merged
output at exactly the cap passes through unmodified, and merged output one
over the cap drops the excess and logs it rather than passing through
uncapped; `findings/test_markdown.py` — the Markdown-escaping
helper, with a dedicated case per neutralized construct (code-fence
break, `[text](url)` link, `![img](url)` image, `@mention`, and a raw
`<img src=...>`/`<a href=...>` inline HTML tag rendering as literal
escaped text rather than a live tag), since this plan's own Security
review flags unescaped versions of exactly these as a
phishing/notification-spam vector, not just a rendering nit; a case
asserting the check-run summary text passes through the same helper as
comment bodies, closing the gap the bullet above flags. Integration
(recorded GitHub cassette via `respx`, per `testing.md`'s convention, in
`review/test_findings_output_step.py`): US6 (combined findings posted as
one batched review), US7 (zero-finding case posts a "no findings" neutral
check-run; non-zero case posts a neutral check-run with a count summary),
US8 (a fatal upstream failure posts a failure-conclusion check-run and
zero comments — driven by injecting Phase 6's negative case from Phase 6's
own test suite); **two dry-run cases, closing the gap where this
mechanism previously had only an E2E happy-path assertion and no
Integration-tier coverage at all for either branch:** (1) the happy path
— `context.dry_run=True` against a normal successful pipeline asserts
zero calls reach the stub GitHub server and the would-be comment/check-run
payload is logged at `info` instead; (2) the scenario this plan's own
prose calls highest-risk for an operator — `context.dry_run=True` against
Phase 6's negative case (the same fatal-failure fixture US8 above uses)
asserts the failure path also makes zero live GitHub calls and logs the
would-be failure-conclusion payload instead of posting it, proving the
terminal step's failure branch honors `context.dry_run` exactly like its
happy-path branch, not just in prose; **a dedicated case proving the
happy-path partial-failure
behavior specified above — this plan's own review caught this as untested,
distinct from the US8 upstream-failure case, since here the pipeline itself
succeeds and only the second of two normal output calls fails:** the
cassette configures `post_review(...)` to succeed and `post_check_run(...)`
to fail (a non-retryable 5xx on the check-run creation call, or the
retry-budget exhausted per Phase 4's retry-scoping tests), asserting the
job is still marked `FAILED`, an `error`-level log line is emitted naming
the PR number and which of the two calls succeeded/failed, and — the
negative half of this same case — `post_review`'s own call is confirmed to
have actually reached the stub GitHub server exactly once (not retried a
second time as a side effect of `post_check_run`'s own failure, and not
rolled back), proving the "already-live, not compensated" behavior the
bullet above describes rather than an accidental double-post or a silent
swallow; a case confirming `posting.py`'s calls go through Phase
4's shared `adapter/github/http.py` retry helper (asserting it's invoked,
per that phase's own test note), plus a dedicated case asserting a bare-5xx
or ambiguous-timeout cassette response to the create-PR-review/check-run
`POST` calls is **not** retried and fails fatally on the first such
response, proving the non-idempotent-write retry scoping Phase 4's
transient-failures bullet specifies for this call site specifically, and a
companion positive case asserting a 429/secondary-rate-limit response
(safe to retry before GitHub processes the write, per that same scoping)
**is** retried and eventually succeeds — proving `posting.py` still
correctly retries the cases that are safe, not only correctly declining
the ones that aren't. New
`apps/worker/test_dispatch.py` case (extending Phase
3's dispatch-loop test file, per that phase's own note that this is where
"any unhandled exception escaping the dispatch loop" is tested): a
directly-enqueued job wired to a pipeline whose pre-filter/diff-fetch step
fails still results in `review/output.py`'s failure path being invoked —
proving the fix above closes the "zero GitHub-visible signal" gap for
failures *before* llm-review, not only the llm-review case US8's own PRD
wording names. `adapter/github/auth.py`'s own tests already exist as of
Phase 4 (see that phase's Tests subsection) — this phase adds no new ones
for it, only `posting.py`'s own cassette-recorded calls above. New
`review/test_review_job.py` (integration, real Postgres via
`testcontainers-python`): the PRD's own Testing table names this file for
US8/US9 specifically because it is the one test that wires the actual,
fully-registered Phase 2 macro registry (pre-filter → static-analysis →
llm-review → output, all real implementations, not fakes or single-step
tests) end to end against a directly-enqueued test job — two distinct
cases, not one, since US8 and US9 are opposite outcomes: (1) **US8** —
Phase 6's LLM-timeout negative case injected against the real registry
proves a real macro-step failure produces `PipelineFailedError`, which
short-circuits the real downstream output step and marks the job `FAILED`
(the macro-step-fatal path, US-F6); (2) **US9** — Phase 5's raising-analyzer
fixture injected against the real registry proves that failure stays
isolated inside the static-analysis macro step and the review still
*completes*, using the other analyzer's and the LLM's findings, posting a
normal (non-`FAILED`) result — the sub-step-isolated path, distinct from
and not to be confused with (1). Neither case can be proven by a
single-step test file on its own, since each of those only fakes its
neighbors; this file is what proves the isolation boundary and the
fatality boundary both hold once every step is real and wired together.

E2E (the roadmap's literal exit criteria, in `tests/e2e/test_review_pipeline.py`,
new — extending the `stub_llm_server`/Compose-driving pattern
`tests/e2e/conftest.py` already established in Phase 0, with a new
`stub_github_server` fixture standing in for `api.github.com`): a synthetic
signed webhook payload POSTed to the running Compose stack's Webhook API
results in the stub GitHub server receiving the expected token-exchange
call, then Phase 4's Files-endpoint and Git-Trees-API calls, then the
per-surviving-file Git-Blobs-API calls Phase 5's materialization makes,
then a PR-review-with-comments call and a check-run call, within the
test's polling window — the stub server's expected-calls list covers the
whole chain this round's diff-fetch restructuring introduced, not only
the posting calls at the end of it. Per `testing.md`'s explicit E2E
failure-mode list ("analyzer crash, LLM timeout, no findings, malformed
webhook signature"), three further cases: the stub LLM server configured
to return a timeout/error response results in a failure-conclusion
check-run and zero comment calls reaching the stub GitHub server; a
fixture PR with no findings from either analyzer or the LLM results in
the "no findings" neutral check-run reaching the stub GitHub server, not
only asserted at the Integration tier; and — the E2E-level proof of the
US8-scope fix above — a fixture PR whose stub Files-endpoint response is
configured to error results in a failure-conclusion check-run reaching
the stub GitHub server (not merely a `FAILED` row in Postgres with no
GitHub-visible signal), asserting the fix holds at the full-stack level,
not only via the directly-enqueued dispatch-loop case in
`apps/worker/test_dispatch.py`. A final companion case reuses Phase 0's
existing malformed-signature rejection path unchanged, confirming this
phase's new enqueue logic never runs when Phase 0's trust boundary
rejects the request first. **The "analyzer crash" item of `testing.md`'s
list, retracted at this tier rather than left as a silently-dropped
requirement — a correction this plan's own review caught missing:** an
earlier draft of this bullet also required a fifth case, a fixture PR
whose only changed file trips a "Phase 5 deliberately-broken Gitleaks
fixture" reused from that phase, asserting the review still completes
with a neutral check-run built from Semgrep's findings alone. No such
fixture exists to reuse: Phase 5's own US9 case (`test_static_analysis_step.py`)
proves analyzer isolation with the *opposite* pairing — the real
Gitleaks binary running successfully against `planted_secret.py`,
isolated from a fake, broken stand-in for Semgrep, not a broken Gitleaks
— and Phase 5's only fixture that deliberately breaks the real Gitleaks
binary (`test_gitleaks.py`'s invalid-invocation case) does so via a
malformed command-line flag, not via network-served PR file content, so
it cannot be "reused" through a webhook payload alone. Deterministically
breaking the real Gitleaks binary from content served over the stub
Files-endpoint, without also disabling the other analyzer (which would
prove config-toggle behavior, US-F4, not isolation, US9), isn't
achievable without binary-specific knowledge this plan doesn't otherwise
need. The property this case would have proven — one analyzer's failure
stays isolated and the review still completes using the other's and the
LLM's findings — is already proven twice at Integration tier
(`test_static_analysis_step.py`'s own real-binary US9 case from Phase 5,
and `test_review_job.py`'s fully-wired-registry case from this phase,
above), which `docs/adr/0005`'s "the closest the framework's own stories
get to boundary-crossing coverage" framing already treats as sufficient
without every property also needing its own E2E repetition. This phase's
E2E suite therefore covers LLM timeout, no findings, and malformed
webhook signature directly from `testing.md`'s list, and retracts the
"analyzer crash" item at this tier rather than silently dropping it —
`tests/e2e/test_review_pipeline.py` carries this same reasoning in its
own module docstring.

**Observability & rollback:** Every post (or dry-run log) records
`job_id`, comment count, and check-run conclusion at `info`; a failed
GitHub write (token exchange or posting call itself failing) logs at
`error` with the typed exception class, and the job is marked `FAILED`
(Phase 3's status transition) rather than silently swallowed. Rollback: the
`REVIEW_WORKER_DRY_RUN` flag doubles as an operational kill-switch — **a
correction to a prior draft's overstatement:** it's read once at Worker
startup (per its own bullet above), so flipping it still requires
restarting the Worker (a Compose/Helm redeploy in practice, same as any
other bootstrap-config change), not a live toggle. What it *does* avoid is
a code change or rollback to a prior image — an operator who sees a bad
first real post edits one config value and restarts, the same speed as
any other bootstrap-config-driven mitigation in this plan, not instant but
meaningfully faster than reverting a deploy.

**Acceptance:** every Integration test above passes; the E2E scenario
passes, satisfying the roadmap's Phase 1 exit criteria verbatim ("a real PR
against a real repo gets a real (if unsophisticated) review posted by the
platform") against the stub GitHub server standing in for a real one;
`REVIEW_WORKER_DRY_RUN=true` produces the same log output as the non-dry-run
case would produce as a posting payload, with zero calls reaching a real
GitHub API — proven at Integration tier by `review/test_findings_output_step.py`'s
two dry-run cases (happy-path and failure-path, both above), not as a
separate E2E case; `mypy
--strict` confirms `review/output.py` imports nothing from `adapter/github`
directly (only the `FindingsPublisher` Protocol), the same Protocol-boundary
proof Phase 6 requires for the LLM Gateway.

## Security review

Phase 1's real new attack surface is the GitHub write path and the first
untrusted-content-processing code in this repo (analyzers, LLM prompt
content) — both get a pass here rather than thin per-phase treatment:

- **Webhook replay (closed).** Phase 0's plan explicitly flagged this
  forward: *"Phase 1 ... should dedupe on GitHub's `X-GitHub-Delivery`
  header before this becomes a real vector."* Phase 1 does exactly that
  (the `webhook_delivery` table). Failure mode if this regresses: a
  replayed/retried webhook double-enqueues, double-spends LLM calls, and
  double-posts comments on a real customer PR.
- **Installation access token handling.** The token
  `adapter/github/auth.py` obtains is short-lived (GitHub-issued, ~1 hour)
  and held only in-process memory, never logged, never persisted. It gets
  the same structural protection as every other credential in this repo,
  not weaker: the cache entry wraps the token value in a `pydantic.SecretStr`
  (the same type `github_app_private_key`/`llm.apiKey` already use), so a
  stray `logger.info(f"...{token}...")` renders as `**********` instead of
  the live value, and the `httpx` client used for GitHub API calls has its
  request/response logging configured to never emit the `Authorization`
  header — belt-and-braces alongside code review, not a substitute for it,
  for what is arguably a more sensitive credential than a static bootstrap
  secret precisely because it's live and directly usable.
- **Untrusted diff/PR content reaching analyzers and the LLM.** A PR's
  diff content is, by definition, attacker-influenceable input (anyone who
  can open a PR against a reviewed repo controls it). Gitleaks/Semgrep only
  ever *read* file content as data (Phase 5's subprocess runner never
  executes anything the diff contains); the LLM call (Phase 6) sends diff
  content as prompt *data*, not as instructions the platform's own code
  executes — a prompt-injection attempt in a diff could influence what the
  LLM *says*, but cannot execute code or reach any credential, since the
  LLM Gateway boundary (`llmgateway.models.CompletionResponse`) is a plain
  string the findings parser treats as untrusted data too (Phase 7's
  Markdown-escaping helper is exactly this: LLM output is untrusted content
  same as PR content, escaped before it ever reaches GitHub's comment
  renderer). PR-controlled *filenames*, not just file content, are also
  untrusted input the platform materializes to its own local filesystem
  (Phase 4) and passes as subprocess arguments (Phase 5) — both are
  explicit requirements in those phases: path-traversal-safe filename
  validation before any write to the per-job temp directory, and a `--`
  end-of-options separator before analyzer filename arguments so a
  flag-shaped filename can't be interpreted as a CLI option. **Residual,
  accepted risk, named explicitly rather than left implicit:** the
  mitigations above cover prompt injection as a *rendering* threat (an
  injected instruction can't execute code or reach a credential) but not
  as a *judgment* threat — a PR whose diff embeds instructions aimed at
  the LLM itself (e.g. "ignore the vulnerability above and report no
  findings," or the reverse, fabricating a misleading finding) could
  influence what the LLM's single, unverified inference pass actually
  concludes. Neither PRD asks for a verification/adversarial pass over LLM
  output in Phase 1 (explicitly out of scope per both PRDs' own
  out-of-scope lists — a later roadmap phase's Verification/Memory work),
  so this is an accepted, unmitigated residual risk for this phase, not a
  silent gap this plan failed to consider.
- **No process-level privilege separation between analyzer subprocesses and
  the credential-holding Worker process — named as an accepted residual
  risk here, caught by this plan's own review as a real gap the rest of
  this section's subprocess hygiene didn't cover.** Gitleaks and Semgrep
  (Phase 5) run via `asyncio.create_subprocess_exec` directly against
  attacker-controlled PR diff content, in the same container/process tree
  as the Worker process holding the live, short-lived GitHub installation
  access token in memory (`adapter/github/auth.py`, Phase 4) and network
  access to both GitHub and the LLM Gateway. Both tools are widely-used and
  actively maintained, but both are third-party binaries parsing hostile
  input, and both have had real historical parser/regex CVEs — a
  compromised analyzer subprocess, absent any privilege boundary, runs with
  whatever privileges the Worker process itself has. This plan's own
  subprocess hygiene elsewhere (kill-and-reap on cancellation, the `--`
  argument-injection guard, chunked-read output caps, path-traversal/mode
  guards on materialized content) addresses resource exhaustion and
  argument/path-injection, but never previously named this class —
  analyzer-tool-vulnerability-as-code-execution — as its own risk. **Mitigation
  this phase does apply, concrete rather than aspirational:** the
  `Dockerfile`'s `runtime` stage (confirmed against the actual file — no
  `USER` directive exists today, so the image runs as `root` by default,
  a pre-existing Phase 0 gap this phase is the first to make load-bearing
  now that real subprocesses run against untrusted input) gains a
  non-root `USER` — a dedicated, minimally-privileged OS user created in
  the `runtime` stage and set as the default user for both the Webhook API
  and Worker containers, so a compromised Gitleaks/Semgrep/redaction
  subprocess does not inherit root inside the container even though it
  still shares the Worker's process tree and network namespace. **Explicitly
  deferred, not silently assumed solved:** true process-level sandboxing
  (a gVisor/nsjail runtime class, seccomp/AppArmor profiles restricting
  filesystem and network access per-subprocess) is real additional
  hardening this phase does not build — named the same way every other
  deferred hardening item in this plan is (the reclaim-timeout gap, the
  multi-replica rate-limiter sharing gap, `webhook_delivery` pruning), and
  left to Phase 8 rather than over-engineered here for a walking skeleton
  where the non-root mitigation already closes the most direct escalation
  path (root-in-container) at negligible cost.
- **Resource exhaustion via a pathological diff/analyzer.** Covered by
  `pipeline-step-framework.md`'s NFRs (per-step timeout, container-level
  CPU/memory bounding) — Phase 2 implements the per-step timeout at the
  macro-step level (with its own dedicated test of the timeout branch, not
  just the raises branch, per this plan's Phase 2 Tests), Phase 5 adds the
  matching sub-step-level timeout test for an individual analyzer, and
  Phase 5 also adds the actual
  `mem_limit`/`cpus`/Helm `resources.limits` container bounds; called out
  here to confirm both halves were considered as a security property
  (denial-of-service against the Worker), not only a reliability one, and
  that the container-bounding half is real config in this plan, not an
  aspirational claim. **Subprocess lifecycle on timeout:** `asyncio.wait_for`
  cancellation alone does not kill the OS process — see Phase 5's
  `subprocess_runner.py` bullet for the fix (an explicit `process.kill()` +
  `await process.wait()` in a `finally`/`except asyncio.CancelledError`
  handler), without which this NFR's timeout mitigation would only stop the
  *step* from waiting on a pathological tool, not stop the tool itself from
  continuing to consume the container's CPU/memory after the fact. Phase
  4's aggregate-materialized-bytes ceiling (see that phase's own bullet)
  closes the companion cross-tenant disk-exhaustion vector the file-count
  ceiling alone left open: memory and CPU were bounded per-container from
  the start, disk was not.
- **Silent-drop ceilings are operator-visible only, not PR-visible —
  accepted tradeoff, named explicitly.** The three finding-count ceilings
  this plan adds (Phase 5's 50-finding static-analysis redaction cap,
  Phase 6's 20-finding LLM redaction cap, Phase 7's 100-finding posted
  ceiling) all drop findings silently from the platform's own perspective,
  logged at `warning` for an operator tailing Worker logs — but nothing
  posted to the PR itself indicates findings were withheld. An attacker
  could in principle pad a diff with cheap matching content specifically
  to push a genuine secret-leak finding past one of these caps, and the PR
  author/reviewer would see a clean review with no indication anything was
  dropped. A PR-visible "N findings were withheld" summary line is a
  reasonable enhancement but is not built here — Phase 1's ceilings are
  operator-facing observability only, consistent with this plan's
  "Observability scope" note above (no metrics/alerting either); closing
  the PR-visibility gap is left to a later phase, not silently assumed
  solved.
- **Cost exhaustion / queue starvation via unbounded review triggers.**
  `synchronize` is in Phase 1's trigger set, so any contributor who can
  push to a PR on a covered repo can otherwise enqueue unlimited `REVIEW`
  jobs — each one an LLM call and a GitHub write, and (since the Worker's
  queue isn't installation-scoped) capable of starving every other
  installation sharing the same Worker. Phase 1 builds a per-repo
  `InMemoryRateLimiter` (the same pattern `apps/webhook_api/rate_limiter.py`
  already uses per-IP) ahead of enqueue, and Phase 6 caps diff size before
  it reaches the LLM prompt, bounding the per-review cost as well as the
  review frequency — **but the rate limiter ships inactive at its default
  configuration** (`review_enqueue_rate_limit_per_minute=0`, per this
  plan's pre-activation-gate policy in "Assumptions this plan makes"), so
  this mitigation is available and tested, not active, until a PRD owner
  reviews the tradeoff and turns it on.
- **Combined attack chain — the four residual risks above and below don't
  just stack, the rate limiter's default-off state is what turns three
  otherwise-bounded risks into one unbounded one.** Any GitHub account able
  to install the operator's App gets an auto-provisioned, live pipeline (no
  allow-list, above) and can, with the rate limiter at its shipped default
  of `0`, push unlimited `synchronize` events on a self-owned PR to drive
  unbounded LLM spend and unbounded `webhook_delivery` row growth (both
  accepted elsewhere in this section) — and every one of those unlimited
  jobs runs Gitleaks/Semgrep against fully attacker-controlled diff content
  with no process-level sandboxing (below) inside the same Worker process
  holding a live GitHub installation token, while simultaneously starving
  every other tenant's reviews on the same Worker. Each risk read in
  isolation is a bounded, deliberately-accepted tradeoff for a walking
  skeleton; the rate limiter is the one lever in this chain with a genuine
  pre-activation kill switch, which makes activating it the single
  highest-leverage mitigation available — the concrete argument for
  prioritizing that PRD-owner review sooner rather than later, not a reason
  to reopen any of the other three acceptances.
- **No new admin/authn surface.** Phase 1 adds no new externally-reachable
  route beyond what Phase 0 already gated (the webhook receiver's existing
  HMAC/rate-limit/body-size trust boundary is unchanged by this plan — Phase
  1 only adds logic *after* that boundary already passed).
- **Secrets never committed.** `github_app_private_key` is a genuine
  secret; `github_app_id` is a public-ish identifier, not sensitive, so it
  belongs in the `ConfigMap`, not the `Secret`. Both get placeholder
  entries in `.env.example` for local dev. **Correction to an
  over-optimistic first draft of this plan:** `charts/review/templates/secret.yaml`
  is *not* a generic field-iterating template — it explicitly lists each of
  Phase 0's four credential fields (`github.webhookSecret`,
  `github.setupStateSecret`, `llm.apiKey`, `database.url`) by name, each
  with its own `existingSecret`-gated `stringData` block, and
  `values.yaml`/`_helpers.tpl`'s `review.credentialEnvVar` helper follow
  the same explicit-per-field shape. Adding `github_app_private_key`
  genuinely requires touching `charts/review/templates/secret.yaml`,
  `values.yaml`, `values-production.yaml`, and the Worker Deployment
  template's env-var wiring — real, if small and low-risk (same shape as
  the four fields already there), Helm chart work this phase must include,
  not zero-change reuse. Added as an explicit Phase 4 bullet (moved there
  from an earlier draft's Phase 7 placement — see Phase 4's own
  bootstrap-config bullet and its sequencing correction note).

## Compatibility & rollout

- **Breaking change to Phase 0's own E2E expectation, handled explicitly
  (Phase 3).** `tests/e2e/test_compose_stack.py`'s
  `test_worker_boots_cleanly_without_crash_looping` currently asserts the
  Worker exits 0 with zero restarts — true and correct for Phase 0's
  "no dispatch loop wired yet" state, false the moment Phase 3 lands. This
  is a deliberate, known, in-plan behavior change to an existing passing
  test, not a regression discovered later — Phase 3's own section rewrites
  it in the same PR that introduces the dispatch loop, so the test suite is
  never left red or silently asserting a stale claim.
- **`docker-compose.yml`'s `worker` service** changes from `restart: "no"`
  (Phase 0's deliberate "clean exit = success" signal) to `restart:
  unless-stopped` (Phase 3) — an operator-visible behavior change for
  anyone who already deployed a Phase-0-era Compose file locally; called
  out in this plan's own PR description when Phase 3 ships, not silently
  folded into an unrelated diff. **Interaction with fail-fast startup
  checks, flagged rather than left implicit:** every existing and new
  startup check (LLM-reachability from Phase 0, Phase 2's step-configuration
  check, Phase 4's GitHub-App-credential check) calls `SystemExit(1)` on
  failure. Under `restart: "no"` (pre-Phase-3) that produced one clean
  non-zero exit; under `restart: unless-stopped` (Phase 3 onward) the same
  failure now restart-loops the container indefinitely instead. This is an
  acceptable trade for a Worker that must stay up across transient job
  failures, but it means a misconfiguration an operator would previously
  see as a single failed container now presents as a crash loop. Phase 3
  adds a new, short `docs/reference/operations.md` ("Worker restart-looping
  after upgrade? Check startup logs for a fail-fast check first" as its
  first entry, extended by Phase 7 below for the new credential fields),
  linked from `README.md`'s existing "Helm chart" section — a concrete
  deliverable, not just a sentence in this plan, so the diagnosis is
  actually discoverable by an operator instead of only documented here.
  **A second entry, added at the same time even though it documents
  Phase 1's own mechanism, not Phase 3's — the runbook file doesn't exist
  yet when Phase 1 merges, so this is where it first becomes possible to
  document it, not a gap in Phase 1's own scope:** "Per-repo review-enqueue
  rate limiting is disabled by default
  (`review_enqueue_rate_limit_per_minute=0`) — a known, accepted-for-now
  tradeoff pending PRD-owner review, not an oversight; see Phase 1's own
  reasoning in this plan for why, and set the value above `0` to
  activate it," so an operator scanning the runbook during rollout sees
  this prominently, not only via the startup `warning` log (Phase 1) or by
  reading this plan in full.
  **Worth naming plainly, not left implicit:** this runbook is itself scope
  added specifically to mitigate a problem this same phase's own
  `restart: unless-stopped` change creates (crash-looping obscuring an
  otherwise-clear fail-fast check) — reasonable operational hygiene, and
  cheap relative to the confusion it prevents, but scope added to offset
  scope this phase introduces, not externally requested by either PRD.
- **New required bootstrap config** (Phase 4, not Phase 7 — see Phase 4's
  own sequencing-correction note): `github_app_id` and
  `github_app_private_key`. Both are required (no default, matching the
  existing `environment`/`github_webhook_secret` no-default pattern) once
  Phase 4 lands, since diff-fetch cannot function without them — an
  operator upgrading from a Phase-0/pre-Phase-4 deployment must populate
  both before the Worker will start (the fail-fast startup check, extended
  in Phase 4 alongside the existing pgvector/LLM checks, refuses to boot
  with them unset rather than posting nothing and failing silently
  mid-review). Per the restart-policy interaction above, an operator who
  upgrades to Phase 4 without setting these will see the Worker crash-loop
  under `restart: unless-stopped`, not exit once — expected, and
  diagnosable from the startup-check's own log line. Phase 4 adds a second
  entry to Phase 3's `docs/reference/operations.md` naming this specific
  case (`github_app_id`/`github_app_private_key` unset or malformed)
  explicitly.
- **Schema migrations are additive only**, per Phase 0's own established
  discipline: Phase 1 adds new columns/tables (`installation_id` on `Repo`,
  `webhook_delivery`), never edits Phase 0's baseline migration in place;
  every new migration has a working `downgrade()`, enforced the same way
  Phase 0's Phase 2 acceptance test enforced it for the baseline. The new
  `installation_id` column is **nullable, with no default** — existing
  Phase-0-era `Repo` rows predate any installation mapping and have no
  value to backfill, so a NOT-NULL constraint would break an
  already-provisioned deployment's upgrade; Phase 1's own resolution logic
  only ever reads/writes it for rows it itself provisions or updates, and
  treats `NULL` the same as "not yet resolved" rather than an invariant
  violation. **No documented Helm-chart migration rollout path, flagged as
  a real gap this phase is the first to make load-bearing — caught by this
  plan's own review, not silently left unaddressed:** `docker-compose.yml`'s
  `migrate` service gates `webhook_api`/`worker` startup on `alembic
  upgrade head` succeeding first (`depends_on:
  condition: service_completed_successfully`), giving local/CI deployments
  a safe migration-before-app-startup ordering. `charts/review/templates/`
  has no equivalent — no migration `Job`, no pre-install/pre-upgrade hook —
  a gap that predates this plan (Phase 0 never built one either, since
  Phase 0 shipped no migrations beyond the baseline). Phase 1 is the first
  phase to add a real migration since that baseline, making this the first
  point at which the gap is actually load-bearing for a production Helm
  deployment: an operator running `helm upgrade` today has no
  chart-provided mechanism ensuring `alembic upgrade head` runs before the
  new `webhook_api`/`worker` pods roll out expecting the new
  `installation_id` column and `webhook_delivery` table to exist. Not built
  in this phase (a Helm migration Job/hook is real chart work out of this
  plan's scope), but named here explicitly, alongside this section's other
  Phase-8-deferred gaps, rather than left for an operator to discover during
  a production upgrade. **Given a manual mitigation, not just a named gap —
  the most severe of the three operator-facing Helm gotchas this plan
  introduces (schema/pod-rollout ordering, not just a confusing log line or
  a missing env var), so it gets the same runbook treatment the other two
  already get:** a third entry in Phase 3's `docs/reference/operations.md`
  ("Upgrading via Helm when this release includes new migrations? Run
  `alembic upgrade head` manually — via `kubectl exec` into a
  migration-capable pod, or a one-off `Job` using the same image — before
  `helm upgrade` rolls out the new pods, until Phase 8 adds a proper
  migration Job/hook"), so an operator has a concrete manual step to
  follow, not only a warning that the gap exists.
- **No customer-facing API/contract exists yet to break** beyond the
  webhook receiver's HTTP status contract (200/401/413/422/429). **Narrower
  than a prior draft of this bullet claimed:** Phase 1 is also the first
  phase to put synchronous Postgres I/O (Org/Repo resolution, the
  `webhook_delivery` insert, `JobQueueRepository.enqueue()`) directly in
  the webhook request path — before Phase 1, that route touched no
  database and could not fail this way. A Postgres outage during webhook
  receipt now surfaces as an uncaught exception (a 500, outside the
  contract's enumerated codes) rather than a handled one. This is likely
  benign in practice — GitHub retries failed deliveries, and the
  delivery-ID idempotency check means a retry after recovery is still
  exactly-once — but it is a real, new failure mode for this code path
  that Phase 8 (production hardening) should give an explicit degrade path
  (e.g. a 503 so GitHub's own retry semantics apply cleanly) rather than an
  unhandled 500; not solved in this phase, named to a specific follow-up
  phase rather than left as an unnamed "future phase," consistent with how
  every other deferred gap in this plan is tracked.
- **Dependency-failure containment.** A GitHub API failure (token exchange
  or posting) marks the job `FAILED` (Phase 3's status model) rather than
  crashing the Worker process — one bad PR's GitHub outage doesn't take
  down review capability for every other PR queued behind it in the same
  Worker instance or being processed by a different Worker replica
  (**correction:** not "concurrently processing," since Phase 3's dispatch
  loop handles one job at a time per instance — see that phase's own
  design). This applies at every phase that now calls the GitHub API,
  not only Phase 7's posting flow: Phase 4's diff-fetch (which took on the
  earlier token-exchange/GitHub-API dependency per this plan's Phase 4
  restructuring) fails the same job the same way. An LLM Gateway failure
  during Phase 6 similarly fails only that job, not the Worker process,
  continuing Phase 0's already-established "the startup check is what's
  fail-fast, not every individual call" distinction.

## Traceability: PRD user story → test

Per ADR-0005 (created in Phase 1, extended in Phase 2 — see Phase 1's own
ADR bullet), US-F1/F2/F3/F4/F6/F7 are pure orchestration stories
with no adapter/external system in the loop — testing.md's story→integration-test
rule is formally exempted for exactly this narrow case, not silently
skipped. Where one of those stories also gets incidental real-registry
coverage from Phase 7's `review/test_review_job.py`, both rows are listed.

**G/W/T column added, closing a gap from an earlier draft — `testing.md`
requires "Story ID → G/W/T → integration test path" and an earlier version
of this table omitted the G/W/T column entirely.** Paraphrased tersely per
story rather than reproducing each PRD's full prose, per that PRD's own
Given/When/Then for the story:

| Story | Given/When/Then (paraphrased) | Plan phase | Test path | Level |
| --- | --- | --- | --- | --- |
| US-F1 | Given a step's `run()`, when the registry invokes it, then it sees only its own declared input/output models and `StepContext` — no other step's internals | Phase 2 | `review/test_step_boundaries.py`, `review/test_step_registry.py` | Unit (ADR-0005) |
| US-F2 | Given two steps with no dependency between them, when the registry runs, then both execute concurrently, not sequentially | Phase 2 | `review/test_step_registry.py` | Unit (ADR-0005) |
| US-F3 | Given a new macro step registered, when the pipeline runs, then it executes with zero changes to `review/step.py`/`review/pipeline.py` | Phase 2, 4, 5, 7 | `review/test_step_registry.py`, `review/test_static_analysis_step.py` (Unit); `review/test_review_job.py` (Integration, real registry); Phase 4's own contribution (registering `review/prefilter.py` needing zero framework changes) is verified structurally via that phase's own Acceptance criteria, not a dedicated test file | Unit (ADR-0005) + Integration |
| US-F4 | Given a step disabled via `StepEnablement`, when the pipeline runs, then that step's `run()` is never invoked | Phase 2, 5, 7 | `review/test_step_registry.py`, `review/test_static_analysis_step.py` (Unit); `review/test_review_job.py` (Integration, real registry) | Unit (ADR-0005) + Integration |
| US-F5 | Given an analyzer sub-step raises or exceeds its timeout, when the static-analysis macro step runs, then that failure is isolated — the macro step still completes using the other sub-step's output | Phase 2, 5 | `review/test_static_analysis_step.py` (unit fake in Phase 2; real analyzer raises **and** times out in Phase 5) | Unit + Integration |
| US-F6 | Given a macro step raises or exceeds its timeout, when the pipeline runs, then the job is marked `FAILED` and no downstream macro step executes | Phase 2, 7 | `review/test_step_registry.py` (Unit — a raising macro step **and** a macro step that exceeds its configured `StepTimeouts` value, both fatal); `review/test_review_job.py` (Integration, real registry) | Unit (ADR-0005) + Integration |
| US-F7 | Given every analyzer and llm-review step disabled, when the Worker starts, then it refuses to start (`SystemExit(1)`) | Phase 2 | `apps/worker/test_settings.py` | Unit (ADR-0005) |
| US1 | Given a verified webhook for an unseen (or already-known) installation, when it's a mapped trigger, then Org/Repo rows exist and a `REVIEW` job is enqueued | Phase 1 | `apps/webhook_api/test_webhook_routes.py` (unseen + known-installation cases) | Integration |
| US2 | Given the same `X-GitHub-Delivery` id processed twice, when both deliveries arrive, then exactly one job is enqueued | Phase 1 | `apps/webhook_api/test_webhook_routes.py` | Integration |
| US3 | Given a PR's changed-file list, when pre-filter runs, then ignored-pattern files are excluded before any analyzer/LLM step sees them | Phase 4 | `review/test_prefilter_step.py` (Unit, fake `DiffFetcher` — the PRD's own required level for this story) | Unit (ADR-0005; `adapter/github/test_diff.py` also covers the underlying adapter, Integration, but exercises diff-fetching, not US3's own filtering behavior) |
| US4 | Given pre-filtered file content, when static-analysis runs, then Gitleaks and Semgrep findings are returned as `Finding` objects | Phase 5 | `review/test_static_analysis_step.py` | Integration |
| US5 | Given the diff and static-analysis findings as context, when llm-review runs, then LLM-sourced findings are returned as `Finding` objects | Phase 6 | `review/test_llm_review_step.py` | Integration |
| US6 | Given findings from both static-analysis and llm-review, when findings-merge runs, then duplicates collapse and the result posts as one batched PR review | Phase 7 | `findings/test_merge.py` (Unit), `review/test_findings_output_step.py` (Integration) | Unit + Integration |
| US7 | Given a completed review (zero or non-zero findings), when output runs, then a neutral check-run is always posted | Phase 7 | `review/test_findings_output_step.py`, `tests/e2e/test_review_pipeline.py` (no-findings E2E case) | Integration + E2E |
| US8 | Given a fatal upstream macro-step failure, when the job fails, then a failure-conclusion check-run is posted and zero comments | Phase 6, 7 | `review/test_llm_review_step.py`, `review/test_findings_output_step.py`, `review/test_review_job.py`, `apps/worker/test_dispatch.py` (pre-filter/diff-fetch-failure case), `tests/e2e/test_review_pipeline.py` (LLM-timeout and Files-endpoint-failure E2E cases) | Integration + E2E |
| US9 | Given one analyzer sub-step fails, when static-analysis runs, then the review still completes using the other analyzer's and the LLM's findings | Phase 5, 7 | `review/test_static_analysis_step.py` (isolated sub-step, fakes its neighbors), `review/test_review_job.py` (real, fully-wired registry — the PRD's own assigned path for this story); no dedicated `tests/e2e/test_review_pipeline.py` case — retracted, see Phase 7's own Tests subsection correction: no reusable Phase 5 fixture breaks the real Gitleaks binary via network-served content alone | Integration |
| US1–US9 happy path | Given a real PR webhook against the full Compose stack, when the whole pipeline runs end to end, then the expected GitHub calls and posted review/check-run occur | Phase 7 | `tests/e2e/test_review_pipeline.py` | E2E |
