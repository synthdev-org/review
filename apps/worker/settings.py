"""Configuration for the Worker entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

Phase 3 of the delivery plan extends this from Phase 1's near-empty model
with the LLM Gateway's bootstrap config (endpoint + credential fields), per
the C4 doc's "Configuration & Secrets" section: the LLM endpoint is
bootstrap config, read once here, never sourced from Postgres. The Webhook
API has no equivalent fields — per the C4 doc's Level 3 diagram, the LLM
Gateway is "used by Worker only."

Phase 2 of the delivery plan adds the pipeline step framework's own
bootstrap config: one enable/disable boolean per macro step and one
timeout-in-seconds field per macro step, plus one shared timeout field
covering both of the static-analysis step's own analyzer sub-steps (see
``review/config.py``'s ``StepEnablement``/``StepTimeouts`` docstrings for
the full reasoning). ``apps/worker/main.py``, the composition root, reads
these fields once here and constructs one ``StepEnablement`` and one
``StepTimeouts`` instance from them.

Phase 3 of the delivery plan adds the storage DSN (``database_url`` — the
same bare ``REVIEW_DATABASE_URL`` variable
``apps/webhook_api/settings.py``'s own field already reuses, per that
phase's "Composition-root wiring for storage" section) and the dispatch
loop's own two bootstrap fields: ``poll_interval_seconds`` (how often to
poll ``JobQueueRepository.claim_next()`` when the queue is empty) and
``stuck_job_warning_threshold_seconds`` (the plain, summed-worst-case-plus-
headroom threshold past which a still-running job gets a ``warning`` log —
see ``apps/worker/dispatch.py``'s own docstring for the full reasoning
behind both defaults).

Phase 4 of the delivery plan adds the GitHub App server-to-server
auth/diff-fetch bootstrap config — moved here from where an earlier plan
draft placed it (Phase 7), since this phase is the first to make real
GitHub API calls under the App's identity load-bearing (see
``adapter/github/auth.py``'s own docstring for the full "why here"
reasoning): ``github_app_id``/``github_app_private_key`` (required, no
default — diff-fetch cannot function without them, and the Worker's
fail-fast startup check, extended this phase, refuses to boot with them
unset or unusable), ``github_api_base_url``/``github_api_timeout_seconds``
(reusing the exact pattern/default ``apps/webhook_api/settings.py``'s
``WebhookAPISettings`` already established for the same purpose), and
``github_retry_max_attempts`` (the GitHub-side retry/backoff policy's own
configurable lever — see ``adapter/github/http.py``'s module docstring).

Phase 5 of the delivery plan adds the static-analysis step's own two
analyzer sub-step enablement flags (``static_analysis_gitleaks_enabled``,
``static_analysis_semgrep_enabled`` — extending ``StepEnablement`` the same
way the four macro-step flags already do, per US-F4) and the independent
``secret_redaction_enabled`` flag (default ``True`` — see
``findings/redact.py``'s module docstring for the full reasoning: this
gates ``findings.redact.TextRedactor``'s own wiring, decoupled from
``static_analysis_gitleaks_enabled`` on purpose, since disabling the
Gitleaks *scanner* sub-step for noise reasons must never silently disable
redaction of a secret the LLM, Phase 6, echoes back verbatim into its own
finding prose).

Phase 6 of the delivery plan adds ``llm_max_diff_bytes`` (the llm-review
step's own diff-size prompt ceiling — see ``review/review_generation.py``'s
module docstring, renamed from ``review/llm_review.py`` by
docs/plans/plan-deliver-verification-pipeline.md's Phase 2 section: diff
size is fully PR-author-controlled and directly drives LLM
token cost, so an oversized PR truncates rather than sending an unbounded
prompt). No new LLM-endpoint fields: ``llm_endpoint``/``llm_api_key``/
``llm_model``/``llm_timeout_seconds`` (Phase 3, above) are reused unchanged
— this phase is the first real *caller* of the already-built
``llmgateway.client.LLMClient``/``LiteLLMClient``, not a second consumer
needing its own config surface.

Phase 4 of the indexing delivery plan (docs/plans/plan-indexing-delivery.md
— a separate plan from the "Phase N" numbering used elsewhere in this
file's own history, disambiguated explicitly here) adds
``embedding_model_path`` — the on-disk directory the Dockerfile's
``embedding-model`` build stage bakes the checksum-verified local
embedding model weights into (``/app/models/all-MiniLM-L6-v2`` by
default, matching that stage's own `COPY --from=embedding-model`
destination in both the ``runtime`` and ``dev`` stages) — read once here
and passed to ``adapter.embedding.local_onnx.LocalONNXEmbeddingClient``,
never read from adapter code directly, per architecture.md's "parse
config once, pass down explicitly" rule.

Phase 7 of the delivery plan adds ``dry_run`` (``REVIEW_WORKER_DRY_RUN``,
per ``docs/specs/phase-1-review-pipeline.md``'s Rollout & Release Safety
section's "decided" dry-run design) — a single bootstrap flag, not
per-step granularity (Phase 1 has exactly one step with an external side
effect to suppress). Read once here at Worker startup and set on Phase 2's
``StepContext`` for every claimed job (``apps/worker/dispatch.py``), not
read directly by ``review/output.py`` from settings — see that module's
own docstring for why threading it through ``context`` rather than reading
settings inside the step keeps this a general mechanism a future
side-effecting step can reuse for free, not a one-off escape hatch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from adapter.product_knowledge.settings import ProductKnowledgeSettingsMixin
from adapter.telemetry.sdk import OtelSettingsMixin
from indexing.clone import GUARANTEE_TOKENS
from review.config import check_static_analysis_budget


class WorkerSettings(OtelSettingsMixin, ProductKnowledgeSettingsMixin):
    """Bootstrap settings for the Worker process.

    `environment` has no default on purpose: an entrypoint that silently
    defaulted it could boot as "development" in production by accident.
    The LLM fields *do* default — to the local LM Studio instance Phase 3's
    Docker Compose target points at (per the C4 doc's Level 3 diagram) —
    because, unlike `environment`, an LLM misconfiguration is caught by
    this phase's own fail-fast startup connectivity check
    (`llmgateway.startup_check.check_llm_reachable`, wired in
    `apps/worker/main.py`) rather than silently running in a wrong mode; a
    production deployment overrides these via Helm `Secret`/`existingSecret`
    values same as any other bootstrap config.

    `llm_api_key` is `pydantic.SecretStr` per the plan's Security review
    ("GitHub App private key, webhook secret, and any LLM API key ... must
    never appear in logs") — never logged or `repr()`-able by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    # --- Storage (Phase 3, adapter/storage) ---
    # Sourced from the bare REVIEW_DATABASE_URL env var, not this class's own
    # REVIEW_WORKER_ prefix — the same variable name WebhookAPISettings'
    # own `database_url` field already reuses (Phase 1) and that Phase 0's
    # `migrate` Compose service and both Helm Deployments already inject.
    # Required: the dispatch loop (apps/worker/dispatch.py) cannot claim or
    # mark jobs without a database.
    database_url: SecretStr = Field(validation_alias="REVIEW_DATABASE_URL")

    llm_endpoint: AnyHttpUrl = AnyHttpUrl("http://host.docker.internal:1234/v1")
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "local-model"
    llm_timeout_seconds: float = 30.0

    # Pipeline step framework (Phase 2, docs/plans/plan-phase-1-walking-skeleton.md):
    # one enable/disable boolean per macro step, mirroring `review.config.StepEnablement`'s
    # field set exactly — `apps/worker/main.py` builds that model from these.
    prefilter_enabled: bool = True
    static_analysis_enabled: bool = True
    # Context Builder delivery plan's Phase 5 section — this step's own
    # kill switch (spec-context-builder.md's Rollout & Release Safety
    # "decided" flag), default `True`: no live config change, a Worker
    # restart/redeploy toggles it, same operational shape as every other
    # per-step enable flag here.
    context_builder_enabled: bool = True
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # the intent_summary step's own kill switch (US-V9), default `True`,
    # mirroring every other macro-step flag's own naming/default convention.
    intent_summary_enabled: bool = True
    # Renamed from `llm_review_enabled` by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # this step is now specifically one of three review-generation-stage
    # steps (intent_summary, review_generation, verification), so the old
    # name would be confusingly narrow. This is a deliberate, documented
    # breaking bootstrap-config rename (see that plan's Compatibility &
    # rollout section) — `apps/worker/main.py`'s
    # `_check_deprecated_settings_env_vars` startup check warns an operator
    # still setting the old `REVIEW_WORKER_LLM_REVIEW_ENABLED` name.
    review_generation_enabled: bool = True
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # the verification step's own kill switch (US-V9), default `True`,
    # mirroring every other macro-step flag's own naming/default convention.
    verification_enabled: bool = True
    output_enabled: bool = True
    # Phase 5: the static-analysis step's own two analyzer sub-steps,
    # toggled independently per US-F4 — mirrors review.config.StepEnablement's
    # own field set exactly.
    static_analysis_gitleaks_enabled: bool = True
    static_analysis_semgrep_enabled: bool = True
    static_analysis_ruff_enabled: bool = True
    static_analysis_eslint_enabled: bool = True
    static_analysis_trivy_enabled: bool = True
    static_analysis_phpstan_enabled: bool = True
    static_analysis_golangci_lint_enabled: bool = True

    # One timeout-in-seconds field per macro step, plus one shared field covering both of
    # the static-analysis step's own analyzer sub-steps — see `review.config.StepTimeouts`'s
    # docstring for why the analyzer sub-steps share a field rather than getting one each,
    # and the plan's Phase 2 section for the reasoning behind each default below.
    prefilter_step_timeout_seconds: int = 30
    static_analysis_step_timeout_seconds: int = 240
    static_analysis_analyzer_timeout_seconds: int = 60
    # Optional per-analyzer override; None inherits the analyzer default above (US-A7).
    static_analysis_ruff_timeout_seconds: int | None = Field(default=None, gt=0)
    static_analysis_eslint_timeout_seconds: int | None = Field(default=None, gt=0)
    static_analysis_trivy_timeout_seconds: int | None = Field(default=None, gt=0)
    static_analysis_phpstan_timeout_seconds: int | None = Field(default=None, gt=0)
    static_analysis_golangci_lint_timeout_seconds: int | None = Field(default=None, gt=0)
    # Worker-process-wide cap on simultaneously running analyzer subprocesses
    # (US-A8); the registry in apps/worker/main.py builds one semaphore of this size.
    static_analysis_max_concurrent_analyzers: int = Field(default=3, ge=1)
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # a short summarization call, lighter than
    # `review_generation_step_timeout_seconds` below (see that plan's US-V9
    # NFR section for the full worst-case-chain-sum reasoning).
    intent_summary_step_timeout_seconds: int = 30
    # Renamed from `llm_review_step_timeout_seconds` by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # mirrors `review_generation_enabled`'s own rename above.
    review_generation_step_timeout_seconds: int = 90
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # a moderate batched-judgment call, lighter than
    # `review_generation_step_timeout_seconds` (see that plan's US-V9 NFR
    # section for the full worst-case-chain-sum reasoning).
    verification_step_timeout_seconds: int = 60
    output_step_timeout_seconds: int = 30
    # Context Builder delivery plan's Phase 5 section (Assumption 7) — two
    # distinct settings with an enforced, validated margin, never the same
    # value: `context_builder_step_timeout_seconds` is the value the outer
    # `review.pipeline.StepRegistry` reads for this step (same shape as
    # every other `*_step_timeout_seconds` field above);
    # `context_builder_internal_timeout_seconds` bounds the *entire*
    # internal orchestration inside `ContextBuilderStep.run()` itself, in
    # one `asyncio.wait_for` — never one `wait_for` per internal call. `int`,
    # not `float` (round seven's own correction — every existing
    # `StepTimeouts` field and its `as_mapping() -> dict[str, int]` return
    # type are `int`-typed; `asyncio.wait_for(timeout=...)` accepts an `int`
    # just as well). The `_context_builder_timeout_margin_is_valid` validator
    # below enforces `context_builder_step_timeout_seconds >
    # context_builder_internal_timeout_seconds` at construction time.
    context_builder_step_timeout_seconds: int = 35
    context_builder_internal_timeout_seconds: int = 25

    # Phase 10 (docs/plans/plan-phase-10.md, Phase 4): the clone_detection step.
    # The step's own internal fail-open timeout is derived in main.py as
    # step timeout minus 5 s (validated below), not a second setting.
    clone_detection_enabled: bool = True
    clone_detection_step_timeout_seconds: int = Field(default=30, gt=0)
    clone_detection_min_tokens: int = Field(default=100, gt=0)
    clone_detection_min_lines: int = Field(default=10, gt=0)
    clone_detection_max_findings: int = Field(default=5, gt=0)

    # Phase 11 (docs/plans/plan-phase-11.md, Phase 3): the fix_generation step, off by
    # default (spec D8). Its internal fail-open timeout is the step timeout minus 5 s.
    fix_generation_enabled: bool = False
    fix_generation_step_timeout_seconds: int = Field(default=90, gt=0)
    fix_generation_max_fixes_per_job: int = Field(default=5, gt=0)
    fix_generation_max_range_lines: int = Field(default=20, gt=0)
    fix_generation_max_replacement_lines: int = Field(default=40, gt=0)
    fix_generation_window_lines: int = Field(default=60, gt=0)

    # Phase 12 (docs/plans/plan-phase-12.md, Phase 3): the walkthrough step, off by
    # default. Companion settings are validated only when the flag is on, so a stale
    # value never blocks a rollback.
    walkthrough_enabled: bool = False
    walkthrough_step_timeout_seconds: int = 45
    walkthrough_min_files: int = 2
    walkthrough_max_files: int = 60
    walkthrough_max_chars: int = 6000

    # --- Job dispatch loop (Phase 3, apps/worker/dispatch.py) ---
    # How often to poll JobQueueRepository.claim_next() when the queue is
    # empty — a concrete default, not left unspecified, per the plan's
    # Phase 3 section.
    poll_interval_seconds: float = 2.0
    # The macro steps run strictly sequentially (see each phase's own "Why
    # here" note), so their worst-case healthy-run duration is the sum of
    # their own configured timeouts above, not their max. Recalculated by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section
    # (US-V9's NFR) now that intent_summary/verification exist: 30
    # (prefilter) + 240 (static_analysis) + 35 (context_builder) + 30
    # (intent_summary) + 90 (review_generation) + 60 (verification) + 30
    # (output) + 30 (clone_detection, Phase 10) + 90 (fix_generation, Phase 11) = 635
    # seconds (+ 45 walkthrough, Phase 12, only when walkthrough_enabled = 680; 3%
    # headroom). 700 gives the 635 sum roughly 10% headroom - comfortably longer than the worst
    # healthy-run total without leaving a genuinely stuck job unwarned for an unreasonable stretch.
    stuck_job_warning_threshold_seconds: float = 700.0

    # --- GitHub App auth / diff fetch (Phase 4, adapter/github/auth.py + diff.py) ---
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
    # every new Worker-side GitHub-calling site this phase and Phase 6/7
    # add (auth.py, diff.py, posting.py) shares these two fields rather
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

    # --- Secret redaction (Phase 5, findings/redact.py) ---
    # Default-active per the plan's Phase 5 section — a baseline-engineering-
    # practice mitigation for a live secret-leak vector, not a product/cost-
    # policy tradeoff (see the plan's "Assumptions this plan makes" section
    # for that distinction), so it ships on by default rather than gated
    # behind prior confirmation. Independent of
    # static_analysis_gitleaks_enabled above on purpose — see this file's
    # own module docstring.
    secret_redaction_enabled: bool = True

    # --- Review-generation step (Phase 6, review/review_generation.py,
    # renamed from review/llm_review.py by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 2 section) ---
    # A fixed byte ceiling on the diff text included in the LLM prompt —
    # roughly a few hundred changed lines, generous for a genuine
    # review-sized PR while bounding worst-case token cost, matching every
    # other numeric bootstrap-config field's pattern. Diff size is fully
    # PR-author-controlled and directly drives LLM token cost, per the
    # plan's Security review.
    llm_max_diff_bytes: int = 200_000

    # --- Local embedding client (indexing delivery plan Phase 4,
    # adapter/embedding/local_onnx.py) ---
    # The on-disk path to the Dockerfile-baked, checksum-verified model
    # directory — see this file's own module docstring. Not a HTTP
    # endpoint/credential like the LLM fields above: this is a purely
    # local filesystem path, no network call at inference time.
    embedding_model_path: str = "/app/models/all-MiniLM-L6-v2"

    # --- Memory reaction polling (docs/plans/plan-deliver-memory.md's
    # Phase 4 section, Unit D, US-M3) ---
    # Bounds worst-case GitHub API call volume per poll independent of how
    # many pushes or findings a PR has accumulated over its lifetime — a
    # concrete ceiling, not unbounded fan-out, per the plan's own "bounded
    # cost, not just bounded per-job" design. A PR with more posted comments
    # than this ceiling simply never has its oldest comments' reactions
    # polled again on a later poll; an accepted, logged gap rather than a
    # cost curve that grows without bound against the installation's shared
    # GitHub API rate limit.
    memory_reaction_poll_max_comments_per_poll: int = 50

    # --- Memory-based suppression (docs/plans/plan-deliver-memory.md's
    # Phase 6 section, Unit F, US-M6/US-M7/US-M9/US-M12) ---
    # Plain `WorkerSettings` fields, not part of `review.config.StepEnablement`/
    # `StepTimeouts` (Assumption 6) — unlike the seven macro steps,
    # similarity suppression isn't a `depends_on`-resolved pipeline stage;
    # it's an internal behavior toggle inside the already-registered
    # `verification` step, read directly by `apps/worker/main.py`'s
    # `_build_review_registry` and passed to `VerificationStep`'s
    # constructor, the same way `llm_max_diff_bytes` already reaches
    # `ReviewGenerationStep` without going through `StepEnablement`.
    # Default `True` — the operational rollback lever (US-M12) is turning
    # this *off*, not opting into it; capture (Phases 2-4) and the admin API
    # (Phase 5) keep running regardless of this flag.
    memory_suppression_enabled: bool = True
    # `memory_similarity_threshold`/`memory_decay_half_life_days` are
    # tunable bootstrap config, not hardcoded constants, per
    # `spec-memory.md`'s own "Resolved decisions" #1 ("tunable later without
    # a schema change... a config value, not a structural one"). `0.90`/
    # `180.0` match the spec's own US-M9 acceptance case and "Resolved
    # decisions" defaults exactly.
    memory_similarity_threshold: float = 0.90
    memory_decay_half_life_days: float = 180.0

    # --- Model routing (docs/plans/plan-deliver-admin-gui.md's Phase 5
    # section, Unit F, US-A14/US-A15/US-A16, Assumption 3) ---
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
    # required" is about a routing *config change*, not this kill switch;
    # see the plan's own "Compatibility & rollout" section) is turning this
    # *off*, which **does** require a `WorkerSettings` env-var change and a
    # Worker restart to flip, unlike a routing config change itself.
    model_routing_config_enabled: bool = True

    # --- Product knowledge (plan-deliver-product-knowledge.md, Phase 6) ---
    # The shared fields (credential map, kill switch, call timeout, private-
    # network switch, allow-list) come from `ProductKnowledgeSettingsMixin`.
    # `product_knowledge_deadline_seconds` bounds the whole provider branch
    # (resolve + negotiate + search + fetch) and is Worker-only. Ordering
    # `call < deadline < context_builder_internal_timeout_seconds` is
    # enforced only when the feature is actually in use (enabled with a
    # credential map), so an operator who tuned the internal timeout to
    # <= 10 s still starts clean on upgrade with nothing configured.
    product_knowledge_deadline_seconds: int = 10

    # --- Findings-merge-and-output step (Phase 7, review/output.py) ---
    # A single bootstrap-config kill-switch: when set, the full pipeline
    # still runs (including this step's own merge/redaction-consuming
    # logic) but every real GitHub write is suppressed and the would-be
    # payload is logged at `info` instead. Defaults to `False` — Phase 1's
    # own shipped default posts real reviews, matching both PRDs' Must-have
    # scope; an operator opts into dry-run explicitly to validate a fresh
    # deployment before trusting it with real writes.
    dry_run: bool = False

    # --- Observability (docs/specs/spec-observability.md's Unit A) ---
    # `otel_exporter_otlp_endpoint`/`otel_exporter_otlp_headers` come from
    # `OtelSettingsMixin` above — shared with `WebhookAPISettings` so the
    # two entrypoints can't silently diverge on this pair's declaration.

    @model_validator(mode="after")
    def _stuck_job_warning_threshold_covers_worst_case_chain(self) -> WorkerSettings:
        """Enforces that ``stuck_job_warning_threshold_seconds`` never drops
        below the macro steps' own worst-case sequential-run sum (the
        635-second figure this field's own docstring comment hand-derives,
        plus ``walkthrough_step_timeout_seconds``, 45 s by default for 680 s
        in total, added only when ``walkthrough_enabled`` is true; keyed on
        the raw flag, not on the step being effective).

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
            + self.clone_detection_step_timeout_seconds
            + self.context_builder_step_timeout_seconds
            + self.intent_summary_step_timeout_seconds
            + self.review_generation_step_timeout_seconds
            + self.verification_step_timeout_seconds
            + self.output_step_timeout_seconds
            + self.fix_generation_step_timeout_seconds
            # Keyed on the raw flag (a superset of "effective"), so flag-off sums are unchanged.
            + (self.walkthrough_step_timeout_seconds if self.walkthrough_enabled else 0)
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

    @field_validator(
        "static_analysis_ruff_timeout_seconds",
        "static_analysis_eslint_timeout_seconds",
        "static_analysis_trivy_timeout_seconds",
        "static_analysis_phpstan_timeout_seconds",
        "static_analysis_golangci_lint_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _blank_engine_timeout_inherits_default(cls, value: object) -> object:
        # Compose passes an unset override as an empty string.
        return None if value == "" else value

    @model_validator(mode="after")
    def _static_analysis_analyzers_fit_step_timeout(self) -> WorkerSettings:
        """US-A7: fail fast when the enabled analyzers' timeouts cannot fit
        the step timeout at the configured concurrency (pure, no I/O)."""
        if not self.static_analysis_enabled:
            return self
        enabled_count = sum(
            value
            for name, value in vars(self).items()
            if name.startswith("static_analysis_")
            and name.endswith("_enabled")
            and name != "static_analysis_enabled"
            and isinstance(value, bool)
        )
        check_static_analysis_budget(
            enabled_analyzer_count=enabled_count,
            max_concurrent=self.static_analysis_max_concurrent_analyzers,
            max_analyzer_timeout_seconds=max(
                self.static_analysis_analyzer_timeout_seconds,
                self.static_analysis_ruff_timeout_seconds or 0,
                self.static_analysis_eslint_timeout_seconds or 0,
                self.static_analysis_trivy_timeout_seconds or 0,
                self.static_analysis_phpstan_timeout_seconds or 0,
                self.static_analysis_golangci_lint_timeout_seconds or 0,
            ),
            step_timeout_seconds=self.static_analysis_step_timeout_seconds,
        )
        return self

    @model_validator(mode="after")
    def _context_builder_timeout_margin_is_valid(self) -> WorkerSettings:
        """Enforces Assumption 7's margin invariant - a pure, cross-field
        numeric comparison with no I/O, the same shape
        ``apps/webhook_api/settings.py``'s existing
        ``_require_setup_state_secret_when_setup_mode_enabled`` validator
        already handles on the Settings model directly (a closer, lighter
        precedent than this file's own I/O-driven startup checks in
        ``apps/worker/main.py``). Two `asyncio.wait_for` calls racing on an
        identical (or inverted) deadline could let the outer, per-step
        timeout win and fail the job before ``ContextBuilderStep``'s own
        internal timeout ever gets a chance to degrade gracefully (US7) -
        this margin is what this phase's own docstring calls "the single
        most important correctness property of this phase."
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
    def _clone_detection_settings_are_valid(self) -> WorkerSettings:
        """The stored fingerprints guarantee detection only for runs of at least
        ``GUARANTEE_TOKENS`` tokens (plan Assumption 1), so ``min_tokens`` may be raised but not
        lowered below it; the derived internal timeout (step timeout minus 5 s)
        must stay positive (Assumption 9). Positivity of each field is the
        ``Field(gt=0)`` constraint above.
        """
        if self.clone_detection_min_tokens < GUARANTEE_TOKENS:
            raise ValueError(
                f"clone_detection_min_tokens ({self.clone_detection_min_tokens}) must be at "
                f"least {GUARANTEE_TOKENS}: stored fingerprints only guarantee detection of "
                "runs that long; lowering it needs a CLONE_FINGERPRINT_VERSION bump"
            )
        if self.clone_detection_step_timeout_seconds <= 5:
            raise ValueError(
                f"clone_detection_step_timeout_seconds "
                f"({self.clone_detection_step_timeout_seconds}) must be greater than 5 - the "
                "step's internal fail-open timeout is the step timeout minus 5 seconds"
            )
        return self

    @model_validator(mode="after")
    def _fix_generation_settings_are_valid(self) -> WorkerSettings:
        """The derived internal timeout (step timeout minus 5 s) must stay positive, and
        the prompt window must hold the longest allowed range plus a line of context on
        each side. Positivity of each field is the ``Field(gt=0)`` constraint above.
        """
        if self.fix_generation_step_timeout_seconds <= 5:
            raise ValueError(
                f"fix_generation_step_timeout_seconds "
                f"({self.fix_generation_step_timeout_seconds}) must be greater than 5 - the "
                "step's internal fail-open timeout is the step timeout minus 5 seconds"
            )
        if self.fix_generation_window_lines < self.fix_generation_max_range_lines + 2:
            raise ValueError(
                f"fix_generation_window_lines ({self.fix_generation_window_lines}) must be at "
                f"least fix_generation_max_range_lines + 2 "
                f"({self.fix_generation_max_range_lines + 2}) so the window can contain the "
                "longest allowed range with context"
            )
        return self

    @model_validator(mode="after")
    def _walkthrough_settings_are_valid(self) -> WorkerSettings:
        """Skipped entirely when ``walkthrough_enabled`` is false (a stale value must never
        block a rollback). Keyed on the raw flag, not step effectiveness. The step timeout
        must leave room for the LLM call (``llm_timeout_seconds``) plus 15 s to redact and publish.
        """
        if not self.walkthrough_enabled:
            return self
        suffix = "; fix it or set walkthrough_enabled=false"
        if self.walkthrough_min_files < 1:
            raise ValueError(
                f"walkthrough_min_files ({self.walkthrough_min_files}) must be at least 1{suffix}"
            )
        if not 1 <= self.walkthrough_max_files <= 200:
            raise ValueError(
                f"walkthrough_max_files ({self.walkthrough_max_files}) must be between 1 and "
                f"200{suffix}"
            )
        if not 500 <= self.walkthrough_max_chars <= 60000:
            raise ValueError(
                f"walkthrough_max_chars ({self.walkthrough_max_chars}) must be between 500 and "
                f"60000{suffix}"
            )
        if self.walkthrough_step_timeout_seconds < self.llm_timeout_seconds + 15:
            raise ValueError(
                f"walkthrough_step_timeout_seconds ({self.walkthrough_step_timeout_seconds}) "
                f"must be at least llm_timeout_seconds + 15 ({self.llm_timeout_seconds + 15})"
                f"{suffix}"
            )
        return self

    @model_validator(mode="after")
    def _product_knowledge_credentials_and_timeouts_are_valid(self) -> WorkerSettings:
        """
        Fail fast on a malformed credential map and enforce the deadline ordering.

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
