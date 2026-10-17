"""Unit tests for review.config — pure, no-I/O logic (per
docs/adr/0005-pure-no-io-logic-unit-tested.md's scope, extended by
docs/plans/plan-deliver-verification-pipeline.md's Phase 5 section):
`resolve_review_pipeline_depends_on` (Assumption 6's hard-coded-to-this-
one-chain resolver), `intent_summary_can_produce_value`'s own two-input
truth table, and `check_step_configuration_viable`'s new additive check.

The pre-existing hand-written checks (`static_analysis_enabled and not
prefilter_enabled`; `review_generation_enabled and not
sa_can_produce_findings and not prefilter_enabled`; the all-disabled and
output-disabled checks) already have dedicated coverage in
`apps/worker/test_settings.py` (this phase extends that file's own field
names/env vars for the `llm_review_*` -> `review_generation_*` rename,
without otherwise touching its test bodies) — this file adds a lighter,
`StepEnablement`-only regression check for the same two hand-written cases
(no `WorkerSettings`/env vars needed for a pure `review.config` predicate),
so this file's own coverage of `check_step_configuration_viable` doesn't
depend on cross-referencing that other file to know the existing checks
still reject what they rejected before this phase.
"""

from __future__ import annotations

import pytest

import review.config as config_module
from review.config import (
    NoViableStepConfigurationError,
    StepEnablement,
    check_step_configuration_viable,
    clone_detection_can_produce_value,
    intent_summary_can_produce_value,
    resolve_review_pipeline_depends_on,
)

# --- resolve_review_pipeline_depends_on ------------------------------------


def test_all_steps_enabled_resolves_the_ordinary_six_step_chain() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement())

    assert resolved == {
        "clone_detection": "static_analysis",
        "context_builder": "clone_detection",
        "intent_summary": "context_builder",
        "review_generation": "intent_summary",
        "verification": "review_generation",
        "output": "verification",
    }


def test_context_builder_disabled_falls_back_to_static_analysis() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(context_builder_enabled=False))

    assert resolved["intent_summary"] == "clone_detection"
    assert resolved["review_generation"] == "intent_summary"
    assert resolved["verification"] == "review_generation"
    assert resolved["output"] == "verification"


def test_intent_summary_disabled_falls_back_to_context_builder() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(intent_summary_enabled=False))

    assert resolved["context_builder"] == "clone_detection"
    assert resolved["review_generation"] == "context_builder"
    assert resolved["verification"] == "review_generation"
    assert resolved["output"] == "verification"


def test_review_generation_disabled_falls_back_all_the_way_to_static_analysis() -> None:
    """`review_generation_enabled=False` is also `context_builder`'s/
    `intent_summary`'s own one real consumer — disabling it makes both
    steps' own `*_can_produce_value` effective state `False` too (matching
    `enabled_mapping`'s own override in `apps/worker/main.py`), so every
    step downstream of it correctly skips past both, landing on
    `static_analysis` directly, not on either disabled optional step.
    """
    resolved = resolve_review_pipeline_depends_on(StepEnablement(review_generation_enabled=False))

    assert resolved["context_builder"] == "clone_detection"
    assert resolved["intent_summary"] == "clone_detection"
    assert resolved["review_generation"] == "clone_detection"
    assert resolved["verification"] == "clone_detection"
    assert resolved["output"] == "verification"


def test_verification_disabled_falls_back_to_review_generation() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(verification_enabled=False))

    assert resolved["review_generation"] == "intent_summary"
    assert resolved["verification"] == "review_generation"
    assert resolved["output"] == "review_generation"


def test_output_disabled_does_not_change_any_resolved_dependency() -> None:
    """`output_enabled` gates whether `output` itself is ever invoked, not
    what it depends on — this predicate has no opinion on that."""
    resolved = resolve_review_pipeline_depends_on(StepEnablement(output_enabled=False))

    assert resolved == resolve_review_pipeline_depends_on(StepEnablement())


def test_static_analysis_cannot_produce_findings_falls_back_to_prefilter() -> None:
    resolved = resolve_review_pipeline_depends_on(
        StepEnablement(
            static_analysis_gitleaks_enabled=False,
            static_analysis_semgrep_enabled=False,
            static_analysis_ruff_enabled=False,
            static_analysis_eslint_enabled=False,
            static_analysis_trivy_enabled=False,
            static_analysis_phpstan_enabled=False,
            static_analysis_golangci_lint_enabled=False,
        )
    )

    assert resolved["clone_detection"] == "prefilter"
    assert resolved["context_builder"] == "clone_detection"
    assert resolved["intent_summary"] == "context_builder"
    assert resolved["review_generation"] == "intent_summary"


def test_context_builder_and_intent_summary_disabled_together_still_resolves_correctly() -> None:
    """Both optional steps ahead of `review_generation` disabled at once —
    proves the resolver actually cascades past *two* consecutive disabled
    steps, not just one."""
    resolved = resolve_review_pipeline_depends_on(
        StepEnablement(context_builder_enabled=False, intent_summary_enabled=False)
    )

    assert resolved["intent_summary"] == "clone_detection"
    assert resolved["review_generation"] == "clone_detection"


# --- intent_summary_can_produce_value's own two-input truth table -----------


@pytest.mark.parametrize(
    ("intent_summary_enabled", "review_generation_enabled", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_intent_summary_can_produce_value_truth_table(
    intent_summary_enabled: bool, review_generation_enabled: bool, expected: bool
) -> None:
    enablement = StepEnablement(
        intent_summary_enabled=intent_summary_enabled,
        review_generation_enabled=review_generation_enabled,
    )

    assert intent_summary_can_produce_value(enablement) is expected


# --- check_step_configuration_viable: pre-existing checks still hold -------


def test_static_analysis_enabled_with_prefilter_disabled_still_rejected() -> None:
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(
            StepEnablement(static_analysis_enabled=True, prefilter_enabled=False)
        )


def test_review_generation_falling_back_to_disabled_prefilter_still_rejected() -> None:
    with pytest.raises(NoViableStepConfigurationError):
        check_step_configuration_viable(
            StepEnablement(
                static_analysis_enabled=False,
                review_generation_enabled=True,
                prefilter_enabled=False,
            )
        )


def test_viable_configuration_still_passes() -> None:
    # Must not raise: the ordinary, all-default configuration.
    check_step_configuration_viable(StepEnablement())


# --- check_step_configuration_viable: new additive check --------------------


def test_additive_check_catches_a_resolved_dependency_pointing_at_a_disabled_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the additive check's own generic mechanism — "is every
    effectively-enabled step's resolved dependency also effectively
    enabled?" — actually fires, using dependency injection to simulate a
    resolver bug that a *correct* `resolve_review_pipeline_depends_on`
    never actually produces (this codebase's real resolver always cascades
    past a disabled optional step to the nearest one that's actually
    enabled, per the tests above) but that neither of the two hand-written
    checks above would catch on their own, since neither one inspects
    `intent_summary_enabled`/`context_builder_enabled`/`verification_enabled`
    at all. This is exactly the class of bug the additive check exists to
    guard against if a future change to the resolver ever reintroduces one.
    """
    enablement = StepEnablement(intent_summary_enabled=False)

    # Neither hand-written check above rejects this configuration on its
    # own: static_analysis/prefilter are both enabled by default, and
    # review_generation/prefilter/static_analysis are unaffected by
    # intent_summary_enabled.
    def _broken_resolver(_: StepEnablement) -> dict[str, str]:
        return {
            "context_builder": "static_analysis",
            "intent_summary": "static_analysis",
            # Bug: points at "intent_summary", which is disabled here —
            # the real resolver would never do this (it falls back to
            # "context_builder" instead, per
            # test_intent_summary_disabled_falls_back_to_context_builder
            # above).
            "review_generation": "intent_summary",
            "verification": "review_generation",
            "output": "verification",
        }

    monkeypatch.setattr(config_module, "resolve_review_pipeline_depends_on", _broken_resolver)

    with pytest.raises(NoViableStepConfigurationError, match="review_generation"):
        check_step_configuration_viable(enablement)


def test_additive_check_does_not_reject_the_real_resolvers_own_output() -> None:
    """The real (unpatched) resolver never trips the additive check for any
    configuration that survives the two hand-written checks above — see the
    `resolve_review_pipeline_depends_on` test matrix above, which exercises
    every individually-disabled combination the additive check would
    otherwise need to catch."""
    for enablement in (
        StepEnablement(),
        StepEnablement(context_builder_enabled=False),
        StepEnablement(intent_summary_enabled=False),
        StepEnablement(review_generation_enabled=False),
        StepEnablement(verification_enabled=False),
        StepEnablement(context_builder_enabled=False, intent_summary_enabled=False),
    ):
        check_step_configuration_viable(enablement)  # must not raise


# --- Phase 9 / Phase 1: generalized analyzer flags and the budget check ---


def test_analyzer_flags_cover_every_static_analysis_flag_field() -> None:
    from review.config import StepEnablement

    enablement = StepEnablement(static_analysis_semgrep_enabled=False)
    assert enablement.analyzer_flags() == {
        "gitleaks": True,
        "semgrep": False,
        "ruff": True,
        "eslint": True,
        "trivy": True,
        "phpstan": True,
        "golangci_lint": True,
    }
    assert enablement.as_mapping()["semgrep"] is False


def test_timeouts_mapping_carries_the_inherited_analyzer_default() -> None:
    from review.config import StepTimeouts

    assert (
        StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()[
            "static_analysis_analyzer"
        ]
        == 7
    )


def test_budget_check_accepts_a_fit_and_rejects_an_overrun() -> None:
    import pytest

    from review.config import check_static_analysis_budget

    check_static_analysis_budget(
        enabled_analyzer_count=9,
        max_concurrent=3,
        max_analyzer_timeout_seconds=60,
        step_timeout_seconds=240,
    )
    with pytest.raises(ValueError, match="exceeds static_analysis_step_timeout_seconds"):
        check_static_analysis_budget(
            enabled_analyzer_count=13,
            max_concurrent=3,
            max_analyzer_timeout_seconds=60,
            step_timeout_seconds=240,
        )


def test_ruff_timeout_inherits_the_analyzer_default_unless_overridden() -> None:
    from review.config import StepTimeouts

    assert StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()["ruff"] == 7
    assert (
        StepTimeouts(
            static_analysis_analyzer_timeout_seconds=7, static_analysis_ruff_timeout_seconds=20
        ).as_mapping()["ruff"]
        == 20
    )


def test_eslint_timeout_inherits_the_analyzer_default_unless_overridden() -> None:
    from review.config import StepTimeouts

    assert StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()["eslint"] == 7
    assert (
        StepTimeouts(
            static_analysis_analyzer_timeout_seconds=7, static_analysis_eslint_timeout_seconds=40
        ).as_mapping()["eslint"]
        == 40
    )


def test_phpstan_timeout_inherits_the_analyzer_default_unless_overridden() -> None:
    from review.config import StepTimeouts

    assert StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()["phpstan"] == 7
    assert (
        StepTimeouts(
            static_analysis_analyzer_timeout_seconds=7, static_analysis_phpstan_timeout_seconds=20
        ).as_mapping()["phpstan"]
        == 20
    )


def test_golangci_lint_timeout_inherits_the_analyzer_default_unless_overridden() -> None:
    from review.config import StepTimeouts

    assert (
        StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()["golangci_lint"] == 7
    )
    assert (
        StepTimeouts(
            static_analysis_analyzer_timeout_seconds=7,
            static_analysis_golangci_lint_timeout_seconds=20,
        ).as_mapping()["golangci_lint"]
        == 20
    )


def test_trivy_timeout_inherits_the_analyzer_default_unless_overridden() -> None:
    from review.config import StepTimeouts

    assert StepTimeouts(static_analysis_analyzer_timeout_seconds=7).as_mapping()["trivy"] == 7
    assert (
        StepTimeouts(
            static_analysis_analyzer_timeout_seconds=7, static_analysis_trivy_timeout_seconds=20
        ).as_mapping()["trivy"]
        == 20
    )


# --- clone_detection (plan-phase-10 Phase 4) --------------------------------


def test_clone_detection_disabled_restores_the_pre_phase_10_chain() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(clone_detection_enabled=False))

    assert resolved["context_builder"] == "static_analysis"
    assert resolved["intent_summary"] == "context_builder"
    assert "clone_detection" in resolved  # still wired (registered, gated by enablement)


def test_clone_detection_can_produce_value_follows_its_own_flag() -> None:
    assert clone_detection_can_produce_value(StepEnablement()) is True
    assert clone_detection_can_produce_value(StepEnablement(clone_detection_enabled=False)) is False


@pytest.mark.parametrize("clone_detection", [True, False])
@pytest.mark.parametrize("context_builder", [True, False])
@pytest.mark.parametrize("analyzers", [True, False])
def test_every_enabled_step_depends_on_the_nearest_enabled_predecessor(
    clone_detection: bool, context_builder: bool, analyzers: bool
) -> None:
    enablement = StepEnablement(
        clone_detection_enabled=clone_detection,
        context_builder_enabled=context_builder,
        static_analysis_gitleaks_enabled=analyzers,
        static_analysis_semgrep_enabled=analyzers,
        static_analysis_ruff_enabled=analyzers,
        static_analysis_eslint_enabled=analyzers,
        static_analysis_trivy_enabled=analyzers,
        static_analysis_phpstan_enabled=analyzers,
        static_analysis_golangci_lint_enabled=analyzers,
    )
    chain = [
        ("prefilter", True),
        ("static_analysis", analyzers),
        ("clone_detection", clone_detection),
        ("context_builder", context_builder),
        ("intent_summary", True),
        ("review_generation", True),
        ("verification", True),
        ("output", True),
    ]
    resolved = resolve_review_pipeline_depends_on(enablement)

    previous = "prefilter"
    for name, enabled in chain[1:]:
        if not enabled:
            continue
        if name in resolved:
            assert resolved[name] == previous, name
        previous = name
    check_step_configuration_viable(enablement)  # every combination is viable


def test_clone_detection_enabled_with_prefilter_disabled_is_rejected() -> None:
    enablement = StepEnablement(
        prefilter_enabled=False,
        static_analysis_enabled=False,
        clone_detection_enabled=True,
        review_generation_enabled=True,
    )

    with pytest.raises(NoViableStepConfigurationError, match="clone_detection"):
        check_step_configuration_viable(enablement)
