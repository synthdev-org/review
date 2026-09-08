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
    intent_summary_can_produce_value,
    resolve_review_pipeline_depends_on,
)

# --- resolve_review_pipeline_depends_on ------------------------------------


def test_all_steps_enabled_resolves_the_ordinary_six_step_chain() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement())

    assert resolved == {
        "context_builder": "static_analysis",
        "intent_summary": "context_builder",
        "review_generation": "intent_summary",
        "verification": "review_generation",
        "output": "verification",
    }


def test_context_builder_disabled_falls_back_to_static_analysis() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(context_builder_enabled=False))

    assert resolved["intent_summary"] == "static_analysis"
    assert resolved["review_generation"] == "intent_summary"
    assert resolved["verification"] == "review_generation"
    assert resolved["output"] == "verification"


def test_intent_summary_disabled_falls_back_to_context_builder() -> None:
    resolved = resolve_review_pipeline_depends_on(StepEnablement(intent_summary_enabled=False))

    assert resolved["context_builder"] == "static_analysis"
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

    assert resolved["context_builder"] == "static_analysis"
    assert resolved["intent_summary"] == "static_analysis"
    assert resolved["review_generation"] == "static_analysis"
    assert resolved["verification"] == "static_analysis"
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
            static_analysis_gitleaks_enabled=False, static_analysis_semgrep_enabled=False
        )
    )

    assert resolved["context_builder"] == "prefilter"
    assert resolved["intent_summary"] == "context_builder"
    assert resolved["review_generation"] == "intent_summary"


def test_context_builder_and_intent_summary_disabled_together_still_resolves_correctly() -> None:
    """Both optional steps ahead of `review_generation` disabled at once —
    proves the resolver actually cascades past *two* consecutive disabled
    steps, not just one."""
    resolved = resolve_review_pipeline_depends_on(
        StepEnablement(context_builder_enabled=False, intent_summary_enabled=False)
    )

    assert resolved["intent_summary"] == "static_analysis"
    assert resolved["review_generation"] == "static_analysis"


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
