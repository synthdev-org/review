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

from pydantic import BaseModel


class StepEnablement(BaseModel):
    """One boolean field per registered macro step (Phase 2), plus
    (Phase 5) one boolean field per static-analysis analyzer sub-step
    (``static_analysis_gitleaks_enabled``, ``static_analysis_semgrep_enabled``)
    — per US-F4, Gitleaks and Semgrep genuinely need to be toggled
    separately.

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
    llm_review_enabled: bool = True
    output_enabled: bool = True
    static_analysis_gitleaks_enabled: bool = True
    static_analysis_semgrep_enabled: bool = True

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
            "llm_review": self.llm_review_enabled,
            "output": self.output_enabled,
            "gitleaks": self.static_analysis_gitleaks_enabled,
            "semgrep": self.static_analysis_semgrep_enabled,
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
    llm_review_step_timeout_seconds: int = 90
    output_step_timeout_seconds: int = 30

    def as_mapping(self) -> dict[str, int]:
        """See ``StepEnablement.as_mapping``'s docstring — the same
        translation, for timeouts. Phase 5's two analyzer sub-step names
        (``gitleaks``, ``semgrep``) both map to the one shared
        ``static_analysis_analyzer_timeout_seconds`` field.
        """
        return {
            "prefilter": self.prefilter_step_timeout_seconds,
            "static_analysis": self.static_analysis_step_timeout_seconds,
            "llm_review": self.llm_review_step_timeout_seconds,
            "output": self.output_step_timeout_seconds,
            "gitleaks": self.static_analysis_analyzer_timeout_seconds,
            "semgrep": self.static_analysis_analyzer_timeout_seconds,
        }


def static_analysis_can_produce_findings(enablement: StepEnablement) -> bool:
    """Whether ``StaticAnalysisStep`` can produce any findings at all under
    ``enablement`` — true only when the macro step itself is enabled *and*
    at least one of its two analyzer sub-steps is. Shared by
    ``check_step_configuration_viable`` below (the fail-fast startup check)
    and ``apps/worker/main.py``'s ``_build_review_registry`` (which uses the
    same predicate to decide whether ``LLMReviewStep`` depends on
    ``"static_analysis"`` or falls back to depending on ``"prefilter"``
    directly) — one definition so the two stay in lockstep; duplicating it
    would let the startup check validate a configuration the registry then
    wires incorrectly.
    """
    return enablement.static_analysis_enabled and (
        enablement.static_analysis_gitleaks_enabled or enablement.static_analysis_semgrep_enabled
    )


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
    static-analysis or llm-review — the two finding-producing macro steps
    — is enabled; pre-filter and output produce no findings of their own.
    **Refined in Phase 5** now that the static-analysis step's own two
    analyzer sub-flags exist: a configuration with ``static_analysis_enabled``
    set but *both* analyzers individually disabled is, for this predicate's
    purposes, equivalent to ``static_analysis_enabled=False`` — the macro
    step would run but every one of its own sub-steps would be skipped,
    producing no findings, so it can no longer act as the "review is still
    possible" leg of this predicate's ``or``.
    """
    sa_can_produce_findings = static_analysis_can_produce_findings(enablement)
    if not sa_can_produce_findings and not enablement.llm_review_enabled:
        raise NoViableStepConfigurationError(
            "no enabled step can produce a review: static-analysis (or both its analyzer "
            "sub-steps) and llm-review are all disabled"
        )
    # StaticAnalysisStep (review/static_analysis.py) declares a fixed
    # depends_on=("prefilter",) — unlike llm_review/output, it has no
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
    # apps/worker/main.py wires LLMReviewStep to depend directly on
    # "prefilter" whenever static-analysis can't itself produce findings
    # (mirroring StaticAnalysisStep's own fixed dependency above) — so the
    # same fail-fast rule applies transitively: prefilter disabled while
    # llm-review is enabled and static-analysis can't produce findings
    # would also fail every review job at runtime.
    if (
        enablement.llm_review_enabled
        and not sa_can_produce_findings
        and not enablement.prefilter_enabled
    ):
        raise NoViableStepConfigurationError(
            "llm-review is enabled but prefilter is disabled and static-analysis cannot "
            "produce findings: llm_review falls back to depending directly on prefilter's "
            "output in that case, so this configuration would fail every review job at "
            "runtime"
        )
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
