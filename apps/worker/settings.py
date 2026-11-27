"""Configuration for the Worker entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

The LLM Gateway's bootstrap config (endpoint + credential fields) lives here,
per the C4 doc's "Configuration & Secrets" section: the LLM endpoint is
bootstrap config, read once here, never sourced from Postgres. The Webhook
API has no equivalent fields — per the C4 doc's Level 3 diagram, the LLM
Gateway is "used by Worker only."

The pipeline step framework's own bootstrap config is one enable/disable
boolean per macro step and one timeout-in-seconds field per macro step, plus
one shared timeout field covering both of the static-analysis step's own
analyzer sub-steps (see ``review/config.py``'s
``StepEnablement``/``StepTimeouts`` docstrings for the full reasoning).
``apps/worker/main.py``, the composition root, reads these fields once here
and constructs one ``StepEnablement`` and one ``StepTimeouts`` instance from
them.

The storage DSN (``database_url``) is the same bare ``REVIEW_DATABASE_URL``
variable ``apps/webhook_api/settings.py``'s own field already reuses. The
dispatch loop has two bootstrap fields of its own: ``poll_interval_seconds``
(how often to poll ``JobQueueRepository.claim_next()`` when the queue is
empty) and ``stuck_job_warning_threshold_seconds`` (the plain,
summed-worst-case-plus-headroom threshold past which a still-running job gets
a ``warning`` log — see ``apps/worker/dispatch.py``'s own docstring for the
full reasoning behind both defaults).

The GitHub App server-to-server auth/diff-fetch bootstrap config lives here
because the Worker is the first component to make real GitHub API calls under
the App's identity load-bearing (see ``adapter/github/auth.py``'s own
docstring for the full "why here" reasoning):
``github_app_id``/``github_app_private_key`` (required, no default —
diff-fetch cannot function without them, and the Worker's fail-fast startup
check refuses to boot with them unset or unusable),
``github_api_base_url``/``github_api_timeout_seconds`` (reusing the exact
pattern/default ``apps/webhook_api/settings.py``'s ``WebhookAPISettings``
already established for the same purpose), and ``github_retry_max_attempts``
(the GitHub-side retry/backoff policy's own configurable lever — see
``adapter/github/http.py``'s module docstring).

The static-analysis step has its own two analyzer sub-step enablement flags
(``static_analysis_gitleaks_enabled``, ``static_analysis_semgrep_enabled`` —
extending ``StepEnablement`` the same way the macro-step flags do, per US-F4)
and an independent ``secret_redaction_enabled`` flag (default ``True`` — see
``findings/redact.py``'s module docstring for the full reasoning: this gates
``findings.redact.TextRedactor``'s own wiring, decoupled from
``static_analysis_gitleaks_enabled`` on purpose, since disabling the Gitleaks
*scanner* sub-step for noise reasons must never silently disable redaction of
a secret the LLM echoes back verbatim into its own finding prose).

``llm_max_diff_bytes`` is the review-generation step's own diff-size prompt
ceiling (see ``review/review_generation.py``'s module docstring): diff size is
fully PR-author-controlled and directly drives LLM token cost, so an
oversized PR truncates rather than sending an unbounded prompt. There are no
extra LLM-endpoint fields for that step: ``llm_endpoint``/``llm_api_key``/
``llm_model``/``llm_timeout_seconds`` are reused unchanged — the step is a
*caller* of the already-built ``llmgateway.client.LLMClient``/
``LiteLLMClient``, not a second consumer needing its own config surface.

``embedding_model_path`` is the on-disk directory the Dockerfile's
``embedding-model`` build stage bakes the checksum-verified local embedding
model weights into (``/app/models/all-MiniLM-L6-v2`` by default, matching that
stage's own `COPY --from=embedding-model` destination in both the ``runtime``
and ``dev`` stages) — read once here and passed to
``adapter.embedding.local_onnx.LocalONNXEmbeddingClient``, never read from
adapter code directly, per architecture.md's "parse config once, pass down
explicitly" rule.

``dry_run`` (``REVIEW_WORKER_DRY_RUN``, per
``docs/specs/spec-review-pipeline.md``'s Rollout & Release Safety section's
"decided" dry-run design) is a single bootstrap flag, not per-step
granularity (the pipeline has exactly one step with an external side effect
to suppress). Read once here at Worker startup and set on the
``StepContext`` for every claimed job (``apps/worker/dispatch.py``), not read
directly by ``review/output.py`` from settings — see that module's own
docstring for why threading it through ``context`` rather than reading
settings inside the step keeps this a general mechanism a future
side-effecting step can reuse for free, not a one-off escape hatch.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from adapter.product_knowledge.settings import ProductKnowledgeSettingsMixin
from adapter.telemetry.sdk import OtelSettingsMixin


class WorkerSettings(OtelSettingsMixin, ProductKnowledgeSettingsMixin):
    """Bootstrap settings for the Worker process.

    `environment` has no default on purpose: an entrypoint that silently
    defaulted it could boot as "development" in production by accident.
    The LLM fields *do* default — to the local LM Studio instance the
    Docker Compose target points at (per the C4 doc's Level 3 diagram) —
    because, unlike `environment`, an LLM misconfiguration is caught by
    the fail-fast startup connectivity check
    (`llmgateway.startup_check.check_llm_reachable`, wired in
    `apps/worker/main.py`) rather than silently running in a wrong mode; a
    production deployment overrides these via Helm `Secret`/`existingSecret`
    values same as any other bootstrap config.

    `llm_api_key` is `pydantic.SecretStr` because the GitHub App private key,
    webhook secret, and any LLM API key must never appear in logs — never
    logged or `repr()`-able by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    # --- Storage (adapter/storage) ---
    # Sourced from the bare REVIEW_DATABASE_URL env var, not this class's own
    # REVIEW_WORKER_ prefix — the same variable name WebhookAPISettings'
    # own `database_url` field already reuses and that the
    # `migrate` Compose service and both Helm Deployments already inject.
    # Required: the dispatch loop (apps/worker/dispatch.py) cannot claim or
    # mark jobs without a database.
    database_url: SecretStr = Field(validation_alias="REVIEW_DATABASE_URL")

    llm_endpoint: AnyHttpUrl = AnyHttpUrl("http://host.docker.internal:1234/v1")
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "local-model"
    llm_timeout_seconds: float = 30.0

    # Pipeline step framework:
    # one enable/disable boolean per macro step, mirroring `review.config.StepEnablement`'s
    # field set exactly — `apps/worker/main.py` builds that model from these.
    prefilter_enabled: bool = True
    static_analysis_enabled: bool = True
    # This step's own kill switch (spec-context-builder.md's Rollout & Release Safety
    # "decided" flag), default `True`: no live config change, a Worker
    # restart/redeploy toggles it, same operational shape as every other
    # per-step enable flag here.
    context_builder_enabled: bool = True
    # The intent_summary step's own kill switch (US-V9), default `True`,
    # mirroring every other macro-step flag's own naming/default convention.
    intent_summary_enabled: bool = True
    # Appends the 12-smell baseline to the review_generation prompt (US-S2 kill
    # switch). Not a pipeline step, so it is not part of `StepEnablement`.
    smell_baseline_enabled: bool = True
    # The one kill switch for the v2 review-quality bundle (prompt, scenario parse rule,
    # extra verification criterion). Per process, read once at start; rollback
    # is false plus a worker restart. Default False (v1).
    review_quality_v2_enabled: bool = False
    # The spec_review step's own kill switch (US-S9), default `True`.
    spec_review_enabled: bool = True
    # Renamed from `llm_review_enabled`:
    # this step is now specifically one of three review-generation-stage
    # steps (intent_summary, review_generation, verification), so the old
    # name would be confusingly narrow. This is a deliberate, documented
    # breaking bootstrap-config rename — `apps/worker/main.py`'s
    # `_check_deprecated_settings_env_vars` startup check warns an operator
    # still setting the old `REVIEW_WORKER_LLM_REVIEW_ENABLED` name.
    review_generation_enabled: bool = True
    # The verification step's own kill switch (US-V9), default `True`,
    # mirroring every other macro-step flag's own naming/default convention.
    verification_enabled: bool = True
    output_enabled: bool = True
    # The static-analysis step's own two analyzer sub-steps,
    # toggled independently per US-F4 — mirrors review.config.StepEnablement's
    # own field set exactly.
    static_analysis_gitleaks_enabled: bool = True
    static_analysis_semgrep_enabled: bool = True
    # Further analyzers, off until fixture precision is checked.
    static_analysis_markdown_links_enabled: bool = False
    static_analysis_sql_migrations_enabled: bool = False

    # One timeout-in-seconds field per macro step, plus one shared field covering both of
    # the static-analysis step's own analyzer sub-steps — see `review.config.StepTimeouts`'s
    # docstring for why the analyzer sub-steps share a field rather than getting one each,
    # and the step specs for the reasoning behind each default below.
    prefilter_step_timeout_seconds: int = 30
    static_analysis_step_timeout_seconds: int = 240
    static_analysis_analyzer_timeout_seconds: int = 60
    static_analysis_markdown_links_timeout_seconds: int = 10
    static_analysis_sql_migrations_timeout_seconds: int = 60
    # A short summarization call, lighter than
    # `review_generation_step_timeout_seconds` below (see US-V9's NFR for
    # the full worst-case-chain-sum reasoning).
    intent_summary_step_timeout_seconds: int = 30
    # Renamed from `llm_review_step_timeout_seconds`;
    # mirrors `review_generation_enabled`'s own rename above.
    review_generation_step_timeout_seconds: int = 90
    # A moderate batched-judgment call, lighter than
    # `review_generation_step_timeout_seconds` (see US-V9's NFR for the
    # full worst-case-chain-sum reasoning).
    # Raised from 60: a batched review judges up to max_diff_chunks + 1 groups
    # (docs/specs/spec-diff-batching.md); see the wave validator below.
    verification_step_timeout_seconds: int = 120
    output_step_timeout_seconds: int = 30
    # spec_review: one extra
    # LLM call plus a spec fetch. The internal timeout (validated below to be
    # strictly lower) makes the step degrade before the registry's FATAL
    # wait_for can fail the whole review (US-S10).
    spec_review_step_timeout_seconds: int = 60
    spec_review_internal_timeout_seconds: int = 45
    # Two
    # distinct settings with an enforced, validated margin, never the same
    # value: `context_builder_step_timeout_seconds` is the value the outer
    # `review.pipeline.StepRegistry` reads for this step (same shape as
    # every other `*_step_timeout_seconds` field above);
    # `context_builder_internal_timeout_seconds` bounds the *entire*
    # internal orchestration inside `ContextBuilderStep.run()` itself, in
    # one `asyncio.wait_for` — never one `wait_for` per internal call. `int`,
    # not `float` (every existing
    # `StepTimeouts` field and its `as_mapping() -> dict[str, int]` return
    # type are `int`-typed; `asyncio.wait_for(timeout=...)` accepts an `int`
    # just as well). The `_context_builder_timeout_margin_is_valid` validator
    # below enforces `context_builder_step_timeout_seconds >
    # context_builder_internal_timeout_seconds` at construction time.
    context_builder_step_timeout_seconds: int = 35
    context_builder_internal_timeout_seconds: int = 25

    # --- Job dispatch loop (apps/worker/dispatch.py) ---
    # How often to poll JobQueueRepository.claim_next() when the queue is
    # empty — a concrete default, not left unspecified.
    poll_interval_seconds: float = 2.0
    # The macro steps run strictly sequentially, so their worst-case
    # healthy-run duration is the sum of their own configured timeouts above,
    # not their max (US-V9's NFR): 30
    # (prefilter) + 240 (static_analysis) + 35 (context_builder) + 30
    # (intent_summary) + 90 (review_generation) + 60 (verification) + 30
    # (output) = 515 seconds. spec_review adds 60
    # seconds: 515 + 60 = 575 seconds, and 660 gives that sum roughly 15%
    # headroom (575 * 1.15 ~= 661, rounded to 660) without leaving a
    # genuinely stuck job unwarned for an unreasonable stretch. Diff batching
    # raised verification 60 -> 120: 635 seconds, still under 660.
    stuck_job_warning_threshold_seconds: float = 660.0

    # --- GitHub App auth / diff fetch (adapter/github/auth.py + diff.py) ---
    # Required, no default: adapter/github/auth.py's JWT signing (the
    # server-to-server App-auth flow) cannot function without the App's own
    # identity and private key. SecretStr per architecture.md's
    # credential-handling rule. Validated at Worker startup
    # (adapter.github.auth.validate_private_key, wired in apps/worker/main.py)
    # by attempting one real RS256 signing call with it — a malformed/
    # truncated key fails loudly at startup, not silently on the first real
    # job's GitHub write.
    github_app_id: int
    github_app_private_key: SecretStr

    # Reuses the exact pattern/default apps/webhook_api/settings.py's
    # WebhookAPISettings already established for the same purpose
    # (consumed there by adapter/github/manifest.py's exchange_temporary_code) —
    # every new Worker-side GitHub-calling site
    # (auth.py, diff.py, posting.py) shares these two fields rather
    # than each hardcoding its own base URL/timeout.
    github_api_base_url: str = "https://api.github.com"
    github_api_timeout_seconds: float = 10.0
    # The GitHub-side retry/backoff policy's own configurable lever (see
    # adapter/github/http.py's module docstring for the full policy this
    # gates): the number of *retries* a GitHub API call is allowed beyond
    # its first attempt. `0` achieves the same effect as disabling the
    # mechanism wholesale — an ordinary validated value, not a magic
    # sentinel needing special handling, since a bounded retry loop
    # naturally no-ops at zero iterations.
    github_retry_max_attempts: int = 3

    # --- Secret redaction (findings/redact.py) ---
    # Default-active — a baseline-engineering-practice mitigation for a live
    # secret-leak vector, not a product/cost-policy tradeoff, so it ships on
    # by default. Independent of
    # static_analysis_gitleaks_enabled above on purpose — see this file's
    # own module docstring.
    secret_redaction_enabled: bool = True

    # --- Review-generation step (review/review_generation.py) ---
    # A fixed byte ceiling on the diff text included in the LLM prompt —
    # roughly a few hundred changed lines, generous for a genuine
    # review-sized PR while bounding worst-case token cost, matching every
    # other numeric bootstrap-config field's pattern. Diff size is fully
    # PR-author-controlled and directly drives LLM token cost.
    llm_max_diff_bytes: int = 200_000

    # --- Diff batching (docs/specs/spec-diff-batching.md) ---
    # An oversize diff is split into related-file batches, one LLM call each.
    # `max_diff_chunks` caps the calls per review (cost); files past the cap are
    # listed as not reviewed. `False` restores the single cut diff.
    diff_batching_enabled: bool = True
    max_diff_chunks: int = Field(default=8, ge=1)
    diff_batch_concurrency: int = Field(default=4, ge=1)
    diff_batch_link_timeout_seconds: int = Field(default=15, ge=1)

    # --- Review context and input shaping (docs/specs/spec-review-context.md) ---
    # Shrink, ordering and the PR map do nothing for a PR that fits one prompt
    # with nothing stubbed. Hunk expansion and verification file context change
    # that prompt, so they default off. Expansion, the map and commit mode need
    # `diff_batching_enabled` (they fetch head content through its fetcher).
    review_shrink_enabled: bool = True
    review_stub_data_min_bytes: int = Field(default=8000, ge=1)
    review_prose_max_bytes: int = Field(default=12000, ge=1)
    review_risk_ordering_enabled: bool = True
    review_pr_map_enabled: bool = True
    review_pr_map_max_bytes: int = Field(default=8000, ge=0)
    review_hunk_expansion_enabled: bool = False
    verification_file_context_enabled: bool = False
    verification_file_context_max_bytes: int = Field(default=24000, ge=1)
    verification_file_context_total_bytes: int = Field(default=80000, ge=1)
    verification_file_context_timeout_seconds: int = Field(default=10, ge=1)
    review_commit_mode_enabled: bool = False
    review_commit_max_commits: int = Field(default=60, ge=1)
    review_commit_fetch_timeout_seconds: int = Field(default=30, ge=1)

    # --- Local embedding client (adapter/embedding/local_onnx.py) ---
    # The on-disk path to the Dockerfile-baked, checksum-verified model
    # directory — see this file's own module docstring. Not a HTTP
    # endpoint/credential like the LLM fields above: this is a purely
    # local filesystem path, no network call at inference time.
    embedding_model_path: str = "/app/models/all-MiniLM-L6-v2"

    # --- Memory reaction polling (US-M3) ---
    # Bounds worst-case GitHub API call volume per poll independent of how
    # many pushes or findings a PR has accumulated over its lifetime — a
    # concrete ceiling, not unbounded fan-out (bounded cost, not just
    # bounded per-job). A PR with more posted comments
    # than this ceiling simply never has its oldest comments' reactions
    # polled again on a later poll; an accepted, logged gap rather than a
    # cost curve that grows without bound against the installation's shared
    # GitHub API rate limit.
    memory_reaction_poll_max_comments_per_poll: int = 50

    # --- Memory-based suppression (US-M6/US-M7/US-M9/US-M12) ---
    # Plain `WorkerSettings` fields, not part of `review.config.StepEnablement`/
    # `StepTimeouts` — unlike the seven macro steps,
    # similarity suppression isn't a `depends_on`-resolved pipeline stage;
    # it's an internal behavior toggle inside the already-registered
    # `verification` step, read directly by `apps/worker/main.py`'s
    # `_build_review_registry` and passed to `VerificationStep`'s
    # constructor, the same way `llm_max_diff_bytes` already reaches
    # `ReviewGenerationStep` without going through `StepEnablement`.
    # Default `True` — the operational rollback lever (US-M12) is turning
    # this *off*, not opting into it; capture and the admin API
    # keep running regardless of this flag.
    memory_suppression_enabled: bool = True
    # `memory_similarity_threshold`/`memory_decay_half_life_days` are
    # tunable bootstrap config, not hardcoded constants, per
    # `spec-memory.md`'s own "Resolved decisions" #1 ("tunable later without
    # a schema change... a config value, not a structural one"). `0.90`/
    # `180.0` match the spec's own US-M9 acceptance case and "Resolved
    # decisions" defaults exactly.
    memory_similarity_threshold: float = 0.90
    memory_decay_half_life_days: float = 180.0

    # --- Model routing (US-A14/US-A15/US-A16) ---
    # Mirrors `memory_suppression_enabled`'s exact four-piece pattern: a
    # plain `WorkerSettings` field with its own rationale docstring (this
    # comment); a one-time startup `WARNING` log when disabled
    # (`apps/worker/main.py`'s `_log_model_routing_disabled_warning`);
    # conditional construction at the composition root (`None` restores
    # today's exact byte-for-byte behavior — every LLM-calling step uses
    # `settings.llm_model` directly, zero `ModelRoutingConfig` reads); and
    # never read directly by step code (`IntentSummaryStep`/
    # `ReviewGenerationStep`/`VerificationStep` only ever see the
    # already-built `model_resolver` object, never this flag itself).
    # Default `True` — the operational rollback lever (US-A16's "no restart
    # required" is about a routing *config change*, not this kill switch) is turning this
    # *off*, which **does** require a `WorkerSettings` env-var change and a
    # Worker restart to flip, unlike a routing config change itself.
    model_routing_config_enabled: bool = True

    # --- Product knowledge ---
    # The shared fields (credential map, kill switch, call timeout, private-
    # network switch, allow-list) come from `ProductKnowledgeSettingsMixin`.
    # `product_knowledge_deadline_seconds` bounds the whole provider branch
    # (resolve + negotiate + search + fetch) and is Worker-only. Ordering
    # `call < deadline < context_builder_internal_timeout_seconds` is
    # enforced only when the feature is actually in use (enabled with a
    # credential map), so an operator who tuned the internal timeout to
    # <= 10 s still starts clean on upgrade with nothing configured.
    product_knowledge_deadline_seconds: int = 10

    # --- Classifier verification ---
    # Pinned defaults only: the per-org setting (mode, model, endpoint, ref)
    # lives in Postgres and the credential comes from the shared credential map. The
    # two semaphores are worker-level (one per process), the timeout is per call.
    # `classifier_allow_private_networks` is off in production (public hosts only,
    # like the Admin probe); the Compose E2E turns it on for its host-process stub.
    classifier_timeout_seconds: int = Field(default=10, ge=1)
    classifier_max_concurrency: int = Field(default=8, ge=1)
    classifier_redaction_concurrency: int = Field(default=4, ge=1)
    classifier_max_state_bytes: int = Field(default=8000, ge=1)
    classifier_allow_private_networks: bool = False

    # --- Findings-merge-and-output step (review/output.py) ---
    # A single bootstrap-config kill-switch: when set, the full pipeline
    # still runs (including this step's own merge/redaction-consuming
    # logic) but every real GitHub write is suppressed and the would-be
    # payload is logged at `info` instead. Defaults to `False` — the
    # shipped default posts real reviews; an operator opts into dry-run
    # explicitly to validate a fresh
    # deployment before trusting it with real writes.
    dry_run: bool = False

    # --- Observability (docs/specs/spec-observability.md) ---
    # `otel_exporter_otlp_endpoint`/`otel_exporter_otlp_headers` come from
    # `OtelSettingsMixin` above — shared with `WebhookAPISettings` so the
    # Worker and Webhook API can't silently diverge on this pair's declaration.

    @model_validator(mode="after")
    def _stuck_job_warning_threshold_covers_worst_case_chain(self) -> WorkerSettings:
        """Enforces that ``stuck_job_warning_threshold_seconds`` never drops
        below the eight macro steps' own worst-case sequential-run sum (the
        515-second figure this field's own docstring comment hand-derives).

        The steps run strictly sequentially, so that sum — not any single
        step's timeout — is the true worst-case healthy-run duration; a
        threshold below it would flag a still-healthy, still-running job as
        "stuck" before it could possibly finish. Enforced here (the same
        pure, cross-field, no-I/O validator shape
        ``_context_builder_timeout_margin_is_valid`` above already
        establishes) rather than left as a comment a future step-timeout
        edit could silently fall out of sync with — the sum is genuinely
        recomputed from the live field values every time settings are
        constructed, not re-typed by hand.
        """
        worst_case_chain_seconds = (
            self.prefilter_step_timeout_seconds
            + self.static_analysis_step_timeout_seconds
            + self.context_builder_step_timeout_seconds
            + self.intent_summary_step_timeout_seconds
            + self.review_generation_step_timeout_seconds
            + self.spec_review_step_timeout_seconds
            + self.verification_step_timeout_seconds
            + self.output_step_timeout_seconds
        )
        if self.stuck_job_warning_threshold_seconds < worst_case_chain_seconds:
            raise ValueError(
                "stuck_job_warning_threshold_seconds "
                f"({self.stuck_job_warning_threshold_seconds}) must be at least the sum of "
                f"every macro step's own timeout ({worst_case_chain_seconds}) - the steps run "
                "strictly sequentially, so a threshold below their worst-case sum would flag "
                "a still-healthy job as stuck before it could possibly finish"
            )
        return self

    @model_validator(mode="after")
    def _batched_steps_fit_their_timeouts(self) -> WorkerSettings:
        """The worst case of a batched review is ``waves * llm_timeout_seconds``
        (plus link building for generation), so each step timeout must cover it.
        """

        def waves(calls: int) -> int:
            return math.ceil(calls / self.diff_batch_concurrency)

        generation_needed = (
            self.diff_batch_link_timeout_seconds
            + waves(self.max_diff_chunks) * self.llm_timeout_seconds
        )
        verification_needed = waves(self.max_diff_chunks + 1) * self.llm_timeout_seconds
        commit_term = ""
        if self.review_commit_mode_enabled:
            generation_needed += self.review_commit_fetch_timeout_seconds
            commit_term = " + review_commit_fetch_timeout_seconds"
        context_term = ""
        if self.verification_file_context_enabled:
            verification_needed += self.verification_file_context_timeout_seconds
            context_term = " + verification_file_context_timeout_seconds"
        if self.review_generation_step_timeout_seconds < generation_needed:
            raise ValueError(
                f"review_generation_step_timeout_seconds "
                f"({self.review_generation_step_timeout_seconds}) must be at least "
                f"{generation_needed}: diff_batch_link_timeout_seconds + "
                "ceil(max_diff_chunks / diff_batch_concurrency) * llm_timeout_seconds"
                f"{commit_term}"
            )
        if self.verification_step_timeout_seconds < verification_needed:
            raise ValueError(
                f"verification_step_timeout_seconds ({self.verification_step_timeout_seconds}) "
                f"must be at least {verification_needed}: "
                "ceil((max_diff_chunks + 1) / diff_batch_concurrency) * llm_timeout_seconds"
                f"{context_term}"
            )
        return self

    @model_validator(mode="after")
    def _context_builder_timeout_margin_is_valid(self) -> WorkerSettings:
        """Enforces the context-builder timeout margin invariant - a pure, cross-field
        numeric comparison with no I/O, the same shape
        ``apps/webhook_api/settings.py``'s existing
        ``_require_setup_state_secret_when_setup_mode_enabled`` validator
        already handles on the Settings model directly (a closer, lighter
        precedent than this file's own I/O-driven startup checks in
        ``apps/worker/main.py``). Two `asyncio.wait_for` calls racing on an
        identical (or inverted) deadline could let the outer, per-step
        timeout win and fail the job before ``ContextBuilderStep``'s own
        internal timeout ever gets a chance to degrade gracefully (US7) -
        this margin is the single most important correctness
        property of the Context Builder's timeout design.
        """
        if (
            self.context_builder_step_timeout_seconds
            <= self.context_builder_internal_timeout_seconds
        ):
            raise ValueError(
                "context_builder_step_timeout_seconds "
                f"({self.context_builder_step_timeout_seconds}) must be strictly greater "
                "than context_builder_internal_timeout_seconds "
                f"({self.context_builder_internal_timeout_seconds}) - the outer step "
                "timeout must leave a real margin over the internal orchestration "
                "timeout, or the outer StepRegistry could time out the whole step before "
                "its own internal timeout ever gets a chance to degrade gracefully"
            )
        return self

    @model_validator(mode="after")
    def _spec_review_internal_timeout_below_step_timeout(self) -> WorkerSettings:
        """The internal deadline must fire first, or the registry's outer
        ``wait_for`` fails the whole review instead of degrading (US-S10).
        """
        if self.spec_review_internal_timeout_seconds >= self.spec_review_step_timeout_seconds:
            raise ValueError(
                "spec_review_internal_timeout_seconds "
                f"({self.spec_review_internal_timeout_seconds}) must be strictly less than "
                f"spec_review_step_timeout_seconds ({self.spec_review_step_timeout_seconds}) - "
                "otherwise the registry's step timeout could fail the review before the "
                "step degrades gracefully"
            )
        if self.spec_review_internal_timeout_seconds <= self.llm_timeout_seconds:
            raise ValueError(
                "spec_review_internal_timeout_seconds "
                f"({self.spec_review_internal_timeout_seconds}) must exceed llm_timeout_seconds "
                f"({self.llm_timeout_seconds}) - otherwise every slow completion is cut off by "
                "the internal deadline and misreported as a degradation"
            )
        return self

    @model_validator(mode="after")
    def _product_knowledge_credentials_and_timeouts_are_valid(self) -> WorkerSettings:
        """Fail fast on a malformed credential map and enforce the deadline ordering.

        Failures name the problem, never the value. Enforces ``call < deadline < internal`` when
        the feature is in use (a non-empty credential map or endpoint allow-list with the kill
        switch on).
        """
        # Why gated, not unconditional: the provider *setting* lives in
        # Postgres (day-two Admin UI config), which settings construction
        # cannot read, so a non-empty credential map is the only bootstrap
        # signal that the feature is in use, and a non-empty endpoint
        # allow-list is the one for an unauthenticated-only provider (it
        # cannot be reached without a credential or an allow-list entry).
        # Validating unconditionally would make the default 5/10/20 s
        # relationship (and any operator tuning of the Context Builder
        # timeout for other reasons) fatal for every install that never
        # enables product knowledge.
        in_use = self.product_knowledge_enabled and bool(
            self.credential_map() or self.endpoint_allowlist()
        )
        if in_use and not (
            self.product_knowledge_call_timeout_seconds
            < self.product_knowledge_deadline_seconds
            < self.context_builder_internal_timeout_seconds
        ):
            raise ValueError(
                "product_knowledge_call_timeout_seconds "
                f"({self.product_knowledge_call_timeout_seconds}) must be less than "
                f"product_knowledge_deadline_seconds ({self.product_knowledge_deadline_seconds}), "
                "which must be less than context_builder_internal_timeout_seconds "
                f"({self.context_builder_internal_timeout_seconds}) - the provider branch "
                "must finish inside the Context Builder's own window"
            )
        return self
