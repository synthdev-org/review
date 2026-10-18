"""review.config — per-step bootstrap configuration for the pipeline
registry (``review/pipeline.py``): which steps are enabled and each step's
own timeout, per the plan's Phase 2 section.

Deliberately **not** ``pydantic_settings.BaseSettings`` — no env var
reading, no infra import — per docs/rules/architecture.md's "domain
packages: zero infra imports" rule and "parse config once at each
entrypoint ... pass it down explicitly" rule.
``apps/worker/settings.py``'s ``WorkerSettings`` is the one place these
values are actually read from the environment; ``apps/worker/main.py``
(the composition root) builds one ``StepEnablement`` and one
``StepTimeouts`` instance from it and passes both into whichever
registry/registries it constructs.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel

_ANALYZER_FLAG = re.compile(r"static_analysis_(.+)_enabled")


class StepEnablement(BaseModel):
    """One boolean field per registered macro step (Phase 2), plus
    (Phase 5) one boolean field per static-analysis analyzer sub-step
    (``static_analysis_gitleaks_enabled``, ``static_analysis_semgrep_enabled``)
    — per US-F4, Gitleaks and Semgrep genuinely need to be toggled
    separately.

    Phase 12: ``secret_redaction_enabled`` is the one non-step input field (it gates
    ``walkthrough_can_produce_value``); it is excluded from ``as_mapping()``.

    ``as_mapping()``'s returned dict carries both macro-step and analyzer-
    sub-step keys in one shape — the same single instance
    ``apps/worker/main.py`` builds once from ``WorkerSettings`` is passed
    both to the outer macro registry (which only looks up its own four
    keys) and, unchanged, into ``review.static_analysis.StaticAnalysisStep``
    (which only looks up ``"gitleaks"``/``"semgrep"``) — mirroring
    ``StepTimeouts.as_mapping()``'s own already-established shape below,
    not a second, narrower mapping built separately for the inner registry.
    """

    prefilter_enabled: bool = True
    static_analysis_enabled: bool = True
    # Phase 10 (docs/plans/plan-phase-10.md): the clone_detection step's kill switch.
    clone_detection_enabled: bool = True
    # Phase 11 (docs/plans/plan-phase-11.md): the fix_generation step; off by default (spec D8).
    fix_generation_enabled: bool = False
    # Phase 12 (docs/plans/plan-phase-12.md, Phase 3): the walkthrough step; off by default.
    walkthrough_enabled: bool = False
    # Not a step and not in as_mapping(): a real secret redactor is a precondition of
    # walkthrough_can_produce_value, so the one answer is shared by resolver, check and registry.
    secret_redaction_enabled: bool = True
    # Added by docs/plans/plan-deliver-context-builder.md's Phase 5 section —
    # the context_builder step's own kill switch (spec-context-builder.md's
    # Rollout & Release Safety "decided" flag), default `True`, mirroring
    # every other macro-step flag's own naming/default convention exactly.
    context_builder_enabled: bool = True
    # Added by docs/plans/plan-deliver-verification-pipeline.md's Phase 5
    # section — the intent_summary step's own kill switch (US-V9), default
    # `True`, mirroring every other macro-step flag's own naming/default
    # convention exactly.
    intent_summary_enabled: bool = True
    # Renamed from `llm_review_enabled` by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # this step is now specifically one of three review-generation-stage
    # steps (intent_summary, review_generation, verification), so the old
    # name would be confusingly narrow.
    review_generation_enabled: bool = True
    # Added by docs/plans/plan-deliver-verification-pipeline.md's Phase 5
    # section — the verification step's own kill switch (US-V9), default
    # `True`, mirroring every other macro-step flag's own naming/default
    # convention exactly.
    verification_enabled: bool = True
    output_enabled: bool = True
    static_analysis_gitleaks_enabled: bool = True
    static_analysis_semgrep_enabled: bool = True
    static_analysis_ruff_enabled: bool = True
    static_analysis_eslint_enabled: bool = True
    static_analysis_trivy_enabled: bool = True
    static_analysis_phpstan_enabled: bool = True
    static_analysis_golangci_lint_enabled: bool = True

    def analyzer_flags(self) -> dict[str, bool]:
        """Every ``static_analysis_<name>_enabled`` field, keyed by analyzer
        ``<name>`` — so adding an engine's flag field is the only edit needed
        for ``as_mapping`` and the can-produce-value predicate to cover it.
        """
        flags: dict[str, bool] = {}
        for field_name in type(self).model_fields:
            match = _ANALYZER_FLAG.fullmatch(field_name)
            if match:
                flags[match.group(1)] = getattr(self, field_name)
        return flags

    def as_mapping(self) -> dict[str, bool]:
        """Translate this model's fixed field set into the plain
        ``Mapping[str, bool]`` ``review.pipeline.StepRegistry`` actually
        consumes, keyed by each concrete step's own ``name`` — see that
        module's own docstring for why the translation lives here rather
        than inside the registry itself.
        """
        return {
            "prefilter": self.prefilter_enabled,
            "static_analysis": self.static_analysis_enabled,
            "clone_detection": self.clone_detection_enabled,
            "fix_generation": self.fix_generation_enabled,
            "walkthrough": self.walkthrough_enabled,
            "context_builder": self.context_builder_enabled,
            "intent_summary": self.intent_summary_enabled,
            "review_generation": self.review_generation_enabled,
            "verification": self.verification_enabled,
            "output": self.output_enabled,
            **self.analyzer_flags(),
        }


class StepTimeouts(BaseModel):
    """One integer-seconds timeout field per registered step, mirroring
    ``StepEnablement``'s shape with one deliberate exception: the two
    analyzer sub-steps share one timeout field
    (``static_analysis_analyzer_timeout_seconds``) rather than one each,
    since neither has a documented reason to need a different time budget
    from the other — enablement and timeout are configured at the
    granularity each one actually needs, not forced into the same shape
    for its own sake. See the plan's Phase 2 section for the reasoning
    behind each default.
    """

    prefilter_step_timeout_seconds: int = 30
    static_analysis_step_timeout_seconds: int = 240
    static_analysis_analyzer_timeout_seconds: int = 60
    # Phase 10: the outer timeout; the step's own internal wait_for is this minus 5 s.
    clone_detection_step_timeout_seconds: int = 30
    # Optional per-analyzer override; None inherits the analyzer default above (US-A7).
    static_analysis_ruff_timeout_seconds: int | None = None
    static_analysis_eslint_timeout_seconds: int | None = None
    static_analysis_trivy_timeout_seconds: int | None = None
    static_analysis_phpstan_timeout_seconds: int | None = None
    static_analysis_golangci_lint_timeout_seconds: int | None = None
    # Added by docs/plans/plan-deliver-context-builder.md's Phase 5
    # section — the value the outer `StepRegistry` reads for this step
    # (distinct from `ContextBuilderStep`'s own internal
    # `context_builder_internal_timeout_seconds`, which never appears here:
    # this model only carries the *outer*, per-step timeout each step's own
    # `Step.run()` call is wrapped in, per `review.pipeline.StepRegistry`).
    context_builder_step_timeout_seconds: int = 35
    # Added by docs/plans/plan-deliver-verification-pipeline.md's Phase 5
    # section — a short summarization call, lighter than
    # `review_generation_step_timeout_seconds` below (see that plan's US-V9
    # NFR section for the full worst-case-chain-sum reasoning).
    intent_summary_step_timeout_seconds: int = 30
    # Renamed from `llm_review_step_timeout_seconds` by
    # docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section —
    # mirrors `StepEnablement.review_generation_enabled`'s own rename.
    review_generation_step_timeout_seconds: int = 90
    # Added by docs/plans/plan-deliver-verification-pipeline.md's Phase 5
    # section — a moderate batched-judgment call, lighter than
    # `review_generation_step_timeout_seconds` (see that plan's US-V9 NFR
    # section for the full worst-case-chain-sum reasoning).
    verification_step_timeout_seconds: int = 60
    output_step_timeout_seconds: int = 30
    # Phase 11: the outer timeout; the step's own internal wait_for is this minus 5 s.
    fix_generation_step_timeout_seconds: int = 90
    # Phase 12: the outer timeout; the step's own internal wait_for is this minus a publish reserve.
    walkthrough_step_timeout_seconds: int = 45

    def as_mapping(self) -> dict[str, int]:
        """See ``StepEnablement.as_mapping``'s docstring — the same
        translation, for timeouts. Phase 5's two analyzer sub-step names
        (``gitleaks``, ``semgrep``) both map to the one shared
        ``static_analysis_analyzer_timeout_seconds`` field.
        """
        return {
            "prefilter": self.prefilter_step_timeout_seconds,
            "static_analysis": self.static_analysis_step_timeout_seconds,
            "clone_detection": self.clone_detection_step_timeout_seconds,
            "context_builder": self.context_builder_step_timeout_seconds,
            "intent_summary": self.intent_summary_step_timeout_seconds,
            "review_generation": self.review_generation_step_timeout_seconds,
            "verification": self.verification_step_timeout_seconds,
            "output": self.output_step_timeout_seconds,
            "fix_generation": self.fix_generation_step_timeout_seconds,
            "walkthrough": self.walkthrough_step_timeout_seconds,
            "gitleaks": self.static_analysis_analyzer_timeout_seconds,
            "semgrep": self.static_analysis_analyzer_timeout_seconds,
            "ruff": self.static_analysis_ruff_timeout_seconds
            or self.static_analysis_analyzer_timeout_seconds,
            "trivy": self.static_analysis_trivy_timeout_seconds
            or self.static_analysis_analyzer_timeout_seconds,
            "eslint": self.static_analysis_eslint_timeout_seconds
            or self.static_analysis_analyzer_timeout_seconds,
            "phpstan": self.static_analysis_phpstan_timeout_seconds
            or self.static_analysis_analyzer_timeout_seconds,
            "golangci_lint": self.static_analysis_golangci_lint_timeout_seconds
            or self.static_analysis_analyzer_timeout_seconds,
            # Default for any analyzer without its own entry (US-A7 inheritance).
            "static_analysis_analyzer": self.static_analysis_analyzer_timeout_seconds,
        }


def static_analysis_can_produce_findings(enablement: StepEnablement) -> bool:
    """Whether ``StaticAnalysisStep`` can produce any findings at all under
    ``enablement`` — true only when the macro step itself is enabled *and*
    at least one of its two analyzer sub-steps is. Shared by
    ``check_step_configuration_viable`` below (the fail-fast startup check)
    and ``apps/worker/main.py``'s ``_build_review_registry`` (which, via
    ``resolve_review_pipeline_depends_on`` below, uses the same predicate to
    decide whether the chain depends on ``"static_analysis"`` or falls back
    to depending on ``"prefilter"`` directly) — one definition so the two
    stay in lockstep; duplicating it
    would let the startup check validate a configuration the registry then
    wires incorrectly.
    """
    return enablement.static_analysis_enabled and any(enablement.analyzer_flags().values())


def check_static_analysis_budget(
    *,
    enabled_analyzer_count: int,
    max_concurrent: int,
    max_analyzer_timeout_seconds: float,
    step_timeout_seconds: float,
) -> None:
    """Raise ``ValueError`` if the enabled analyzers cannot all finish inside
    the step timeout at the configured concurrency: ``ceil(count / N) *
    max(per-analyzer timeout)`` must fit (US-A7/US-A8; analyzers run in waves
    of at most ``N`` since queue time is excluded from each analyzer's own
    timeout).
    """
    waves = math.ceil(enabled_analyzer_count / max_concurrent)
    worst_case = waves * max_analyzer_timeout_seconds
    if worst_case > step_timeout_seconds:
        raise ValueError(
            f"static-analysis worst case ({enabled_analyzer_count} analyzers / "
            f"{max_concurrent} concurrent = {waves} waves x {max_analyzer_timeout_seconds}s = "
            f"{worst_case}s) exceeds static_analysis_step_timeout_seconds "
            f"({step_timeout_seconds}s)"
        )


def context_builder_can_produce_value(enablement: StepEnablement) -> bool:
    """Whether running ``ContextBuilderStep`` can actually produce any value
    under ``enablement`` — true only when the step itself is enabled *and*
    ``review_generation`` is also enabled.

    ``review_generation`` is this predicate's one gating consumer, but as of
    Phase 5's ``verification`` step it is no longer the *only* step that ever
    reads a job's ``context_bundle`` — ``review.verification.VerificationStep``
    also forwards and reads it (see ``review.verification._VerificationInput``).
    That's still safe to gate on ``review_generation_enabled`` alone today:
    ``verification`` always depends (directly or transitively) on
    ``review_generation``'s own output in this codebase's one pipeline chain
    (see ``resolve_review_pipeline_depends_on`` below), so
    ``review_generation`` disabled already means ``verification`` never
    receives a real ``context_bundle`` either, regardless of this predicate.
    If a future change ever lets ``verification`` run independently of
    ``review_generation``, this predicate must be revisited to also consider
    ``enablement.verification_enabled``, or it will silently starve
    ``verification`` of a ``context_bundle`` it needs.

    Without this check, ``context_builder_enabled``'s default of ``True``
    means disabling ``review_generation`` alone (e.g. during an LLM-endpoint
    outage or a cost-control measure) still pays the full cost of every
    ``ContextBuilderStep`` run — diff/tree content fetch, the blast-radius
    graph query, and the embedding+similarity query — for a
    ``ContextBundle`` no step ever consumes. Mirrors
    ``static_analysis_can_produce_findings``'s own shape and reasoning
    above: one shared predicate ``apps/worker/main.py`` uses both to gate
    the step's effective enablement and to keep that decision from drifting
    out of sync with any other place that needs the same answer.
    """
    return enablement.context_builder_enabled and enablement.review_generation_enabled


def clone_detection_can_produce_value(enablement: StepEnablement) -> bool:
    """Whether running ``CloneDetectionStep`` can produce value: its findings are
    consumed by the always-required ``output`` step, so only its own flag matters.
    Kept as a named predicate (like its siblings) so the registry wiring and
    ``check_step_configuration_viable`` share one definition.
    """
    return enablement.clone_detection_enabled


def fix_generation_can_produce_value(enablement: StepEnablement) -> bool:
    """Whether running ``FixGenerationStep`` can produce value: it is enabled and at
    least one eligible finding producer (spec D2) can run - static analysis, or
    LLM review generation together with verification. Shared by the registry wiring
    and ``check_step_configuration_viable`` so the two can't drift.
    """
    return enablement.fix_generation_enabled and (
        static_analysis_can_produce_findings(enablement)
        or (enablement.review_generation_enabled and enablement.verification_enabled)
    )


def walkthrough_can_produce_value(enablement: StepEnablement) -> bool:
    """Whether the walkthrough step is effective: enabled, a review_generation step
    exists (the one configuration where every predecessor carries a diff), and a real
    secret redactor is configured (its posted text is unredacted otherwise).
    """
    return (
        enablement.walkthrough_enabled
        and enablement.review_generation_enabled
        and enablement.secret_redaction_enabled
    )


def intent_summary_can_produce_value(enablement: StepEnablement) -> bool:
    """Whether running ``IntentSummaryStep`` can actually produce any value
    under ``enablement`` — true only when the step itself is enabled *and*
    its one real consumer, ``review_generation``, is also enabled
    (``review_generation`` is the only step that ever reads a job's
    ``intent_summary`` — see ``review.intent_summary.IntentSummaryOutput``'s
    own docstring). Mirrors ``context_builder_can_produce_value``'s own
    shape/reasoning exactly, for the symmetric problem: running
    ``IntentSummaryStep`` while ``review_generation`` is disabled would waste
    a full LLM call producing an ``intent_summary`` field nothing reads.
    """
    return enablement.intent_summary_enabled and enablement.review_generation_enabled


def resolve_review_pipeline_depends_on(enablement: StepEnablement) -> dict[str, str]:
    """Resolves ``depends_on`` for the six dynamically-wired steps in the
    one review pipeline this codebase has (``clone_detection -> context_builder ->
    intent_summary -> review_generation -> verification -> fix_generation -> output``) —
    **one small, concrete, hard-coded-to-this-one-chain function, not a
    general-purpose, arbitrary-chain-length utility** (Assumption 6 of
    docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section: an
    earlier draft's generic ``resolve_chain_depends_on(chain: Sequence[str],
    enabled: Mapping[str, bool])`` utility was correctly flagged as
    speculative generality this codebase's own architecture.md rejects —
    exactly one review pipeline exists, so nothing here accepts or expects a
    different chain).

    Shared by ``check_step_configuration_viable`` (to validate) and
    ``apps/worker/main.py``'s ``_build_review_registry`` (to wire) so the
    two can't drift, the same "one definition" reasoning
    ``static_analysis_can_produce_findings``'s own docstring already gives
    for a simpler, one-predicate case.

    Pure, no I/O — see ``review/test_config.py`` and
    docs/adr/0005-pure-no-io-logic-unit-tested.md's own scope list.
    """
    sa_can_produce_findings = static_analysis_can_produce_findings(enablement)
    upstream_of_clone_detection = "static_analysis" if sa_can_produce_findings else "prefilter"
    upstream_of_context_builder = (
        "clone_detection"
        if clone_detection_can_produce_value(enablement)
        else upstream_of_clone_detection
    )

    context_builder_effectively_enabled = context_builder_can_produce_value(enablement)

    intent_summary_effectively_enabled = intent_summary_can_produce_value(enablement)
    intent_summary_depends_on = (
        "context_builder" if context_builder_effectively_enabled else upstream_of_context_builder
    )

    upstream_of_review_generation = (
        "intent_summary" if intent_summary_effectively_enabled else intent_summary_depends_on
    )

    verification_depends_on = (
        "review_generation"
        if enablement.review_generation_enabled
        else upstream_of_review_generation
    )

    fix_generation_depends_on = (
        "verification" if enablement.verification_enabled else verification_depends_on
    )

    walkthrough_depends_on = (
        "fix_generation"
        if fix_generation_can_produce_value(enablement)
        else fix_generation_depends_on
    )
    output_depends_on = (
        "walkthrough" if walkthrough_can_produce_value(enablement) else walkthrough_depends_on
    )

    return {
        "clone_detection": upstream_of_clone_detection,
        "context_builder": upstream_of_context_builder,
        "intent_summary": intent_summary_depends_on,
        "review_generation": upstream_of_review_generation,
        "verification": verification_depends_on,
        "fix_generation": fix_generation_depends_on,
        "walkthrough": walkthrough_depends_on,
        "output": output_depends_on,
    }


class NoViableStepConfigurationError(Exception):
    """Raised by ``check_step_configuration_viable`` when the enabled-step
    configuration can never produce a review.
    """


def check_step_configuration_viable(enablement: StepEnablement) -> None:
    """Raise ``NoViableStepConfigurationError`` if ``enablement`` can never
    produce a review — the pure half of US-F7's fail-fast startup check.
    The composition root (``apps/worker/main.py``) is the impure half that
    decides ``SystemExit(1)``, mirroring ``llmgateway.startup_check`` and
    ``adapter.storage.pgvector_check``'s own pure-check-plus-``SystemExit``
    split.

    Phase 2's own granularity: a review can only ever be produced if
    static-analysis or review-generation — the two finding-producing macro
    steps — is enabled; pre-filter and output produce no findings of their
    own.
    **Refined in Phase 5** now that the static-analysis step's own two
    analyzer sub-flags exist: a configuration with ``static_analysis_enabled``
    set but *both* analyzers individually disabled is, for this predicate's
    purposes, equivalent to ``static_analysis_enabled=False`` — the macro
    step would run but every one of its own sub-steps would be skipped,
    producing no findings, so it can no longer act as the "review is still
    possible" leg of this predicate's ``or``.
    """
    sa_can_produce_findings = static_analysis_can_produce_findings(enablement)
    if not sa_can_produce_findings and not enablement.review_generation_enabled:
        raise NoViableStepConfigurationError(
            "no enabled step can produce a review: static-analysis (or both its analyzer "
            "sub-steps) and review-generation are all disabled"
        )
    # StaticAnalysisStep (review/static_analysis.py) declares a fixed
    # depends_on=("prefilter",) — unlike review_generation/output, it has no
    # constructor parameter letting the composition root re-point it at a
    # different upstream step when prefilter is disabled. Disabling
    # prefilter while static_analysis stays enabled therefore wires a step
    # whose declared dependency will always report status "disabled",
    # which review.pipeline.StepRegistry treats as a fatal error on every
    # job — not a graceful skip. Reject that combination here, at the same
    # fail-fast startup check, rather than at first-job runtime.
    if enablement.static_analysis_enabled and not enablement.prefilter_enabled:
        raise NoViableStepConfigurationError(
            "static-analysis is enabled but prefilter is disabled: static_analysis has a "
            "fixed dependency on prefilter's output, so this configuration would fail every "
            "review job at runtime"
        )
    # apps/worker/main.py wires ReviewGenerationStep to depend directly on
    # "prefilter" whenever static-analysis can't itself produce findings
    # (mirroring StaticAnalysisStep's own fixed dependency above) — so the
    # same fail-fast rule applies transitively: prefilter disabled while
    # review-generation is enabled and static-analysis can't produce
    # findings would also fail every review job at runtime. Not a
    # hand-written check here (it used to be — dropped once genuinely
    # subsumed): the generic loop below, driven by
    # ``resolve_review_pipeline_depends_on``, already catches exactly this
    # combination (``review_generation``/``context_builder``/
    # ``intent_summary`` all effectively resolve back to a disabled
    # ``prefilter``) — keeping a second, hand-written copy of the same rule
    # risked the two silently diverging if the resolver's own fallback logic
    # ever changed without a matching edit here.
    # The registered `terminal_step` ("output") is the only step that ever
    # posts a review/check-run to GitHub — review.pipeline.StepRegistry
    # treats a disabled step as a silent skip, not a failure, so disabling
    # it produces no PipelineFailedError and no operator-visible signal at
    # all: every job would compute real findings and then post nothing.
    if not enablement.output_enabled:
        raise NoViableStepConfigurationError(
            "output is disabled: no review or check-run would ever be posted, and nothing "
            "would signal that at runtime — every job would silently produce no visible result"
        )
    # Added in Phase 5 (docs/plans/plan-deliver-verification-pipeline.md): a
    # strict superset of the remaining hand-written `static_analysis`/
    # `prefilter` check above, now covering all five dynamically-wired steps
    # uniformly via the one shared resolver both this check and
    # `apps/worker/main.py`'s own wiring use. The one hand-written check this
    # loop fully subsumed (the `review_generation`-falls-back-to-`prefilter`
    # case) was removed rather than kept as a second, hand-maintained copy of
    # the same rule — see the comment above the `output_enabled` check. The
    # `static_analysis`-depends-on-`prefilter` check above stays hand-written
    # because `static_analysis` isn't one of this loop's five dynamically-
    # wired steps (its `depends_on` is fixed, not resolved). Catches, e.g.,
    # `verification` enabled while `review_generation` is disabled
    # (verification would depend on a step that never runs).
    resolved_depends_on = resolve_review_pipeline_depends_on(enablement)
    effectively_enabled: dict[str, bool] = {
        "prefilter": enablement.prefilter_enabled,
        "static_analysis": sa_can_produce_findings,
        "clone_detection": clone_detection_can_produce_value(enablement),
        "context_builder": context_builder_can_produce_value(enablement),
        "intent_summary": intent_summary_can_produce_value(enablement),
        "review_generation": enablement.review_generation_enabled,
        "verification": enablement.verification_enabled,
        "fix_generation": fix_generation_can_produce_value(enablement),
        "walkthrough": walkthrough_can_produce_value(enablement),
        "output": enablement.output_enabled,
    }
    for step_name, target_name in resolved_depends_on.items():
        if effectively_enabled[step_name] and not effectively_enabled[target_name]:
            raise NoViableStepConfigurationError(
                f"{step_name} is enabled but its resolved dependency {target_name!r} is not "
                "— this configuration would fail every review job at runtime"
            )
