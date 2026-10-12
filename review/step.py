"""review.step — the ``Step`` Protocol and the shared ``StepContext`` every
step receives, per the plan's Phase 2 section
(docs/plans/plan-phase-1-walking-skeleton.md) and
docs/specs/pipeline-step-framework.md's Scope section.

A narrow, domain-defined Protocol (1-3 methods, per
docs/rules/architecture.md's Protocol-size rule): ``run(input, context) ->
output``, with ``input``/``output`` specific to each concrete step and
``context`` one shared, step-agnostic model (``job_id`` for log
correlation, ``dry_run`` for suppressing a step's own side effect) passed
identically to every step — so a future side-effecting step reads the same
``context.dry_run`` field rather than getting a new parameter of its own.

Used at **two nested levels** (``review/pipeline.py``'s own docstring has
the full picture): the four macro pipeline steps (pre-filter,
static-analysis, llm-review, findings-merge-and-output — registered by
later phases), and the static-analysis step's own inner registry of
analyzer sub-steps (Gitleaks, Semgrep — Phase 5). Both levels are ordinary
``Step`` implementations to the registry; nothing here distinguishes a
macro step from a sub-step structurally.

Zero infra imports (no ``fastapi``/``sqlalchemy``/``httpx``/``litellm``/
``subprocess``), per architecture.md's domain-package rule — a concrete
step's own implementation (``review/prefilter.py``,
``review/static_analysis.py``, etc., added by later phases) is where those
live, never here or in ``review/pipeline.py``.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

InputT = TypeVar("InputT", contravariant=True)
OutputT = TypeVar("OutputT", covariant=True)


class JobIdentity(BaseModel):
    """The job-identifying fields (which org/repo/installation/PR a job
    is for) that every macro step's own Input and Output model carries
    forward, per ``review.pipeline.StepRegistry``'s single-dependency-input
    design: each step's ``Output`` becomes the next step's ``Input``, so
    the identity has to ride along on every one of them, unchanged, all
    the way to the final findings-output step — see
    ``review/prefilter.py``'s ``PrefilterOutput``,
    ``review/static_analysis.py``'s ``StaticAnalysisOutput``, and
    ``review/review_generation.py``'s ``ReviewGenerationOutput`` docstrings
    for why.

    A concrete step's own Input/Output model inherits this instead of
    repeating the same fields across three files — previously
    duplicated byte-for-byte in ``review/prefilter.py``,
    ``review/static_analysis.py``, and ``review/review_generation.py``. Living in
    ``review/step.py`` (a framework module every step module already
    imports ``Step``/``StepContext`` from, and explicitly exempt from
    ``review/test_step_boundaries.py``'s step-to-step import-boundary
    check) keeps this a framework-level dependency, not a step-to-step one.

    ``base_sha`` (added by
    docs/plans/plan-deliver-context-builder.md's Phase 1 section) is
    **optional** (``str | None = None``), unlike the required fields
    above — a required field here would raise a pydantic ``ValidationError``
    on ``PrefilterStep.run``'s very first ``PrefilterOutput(...)``
    construction for every job, since GitHub's payload can genuinely omit
    ``pull_request.base.sha``. Because it's optional, it is **not**
    automatically carried forward the way the required fields are — each
    step's own ``Output`` construction that should carry it forward must
    still forward it explicitly (``base_sha=input.base_sha``), per that
    plan's Assumption 2.
    """

    org_id: str
    repo_id: str
    installation_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str | None = None
    # Product-knowledge capture (docs/plans/plan-deliver-product-knowledge.md
    # Phase 2, US-PK2/A1). Optional for the same reason ``base_sha`` is, and
    # likewise not auto-carried: hand-constructing sites forward them
    # explicitly. ``pr_is_fork is None`` (old payload) must be treated as
    # "fork" by the exposure gate (fail closed). The PR body is never carried.
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None
    # Whether the repo is private on GitHub — the REVIEW payload's existing
    # ``private`` key (also read by ``apps/worker/dispatch.py`` for usage
    # metering). Defaults to ``False`` so an absent value fails closed at the
    # product-knowledge exposure gate (A1): a repo is only ever treated as
    # private when the webhook said so.
    private: bool = False


def job_identity_fields(source: JobIdentity) -> dict[str, Any]:
    """``source``'s own ``JobIdentity`` fields, as a dict keyed by
    field name — for a step's own Output construction that forwards every
    ``JobIdentity`` field unchanged from its parsed input, without
    hand-listing each keyword argument. Previously duplicated
    byte-for-byte across ``review/review_generation.py``,
    ``review/intent_summary.py``, and ``review/verification.py``'s own
    Output-construction return statements; living here for the same
    "framework module every step already imports from, exempt from the
    step-to-step import-boundary check" reason ``JobIdentity`` itself does
    (see that class's own docstring).
    """
    return source.model_dump(include=set(JobIdentity.model_fields))


class StepContext(BaseModel):
    """Shared, step-agnostic context passed identically to every step's
    ``run()`` call.

    Deliberately minimal — per pipeline-step-framework.md's Scope section,
    ``context`` exists so cross-cutting concerns are available to *any*
    step without widening the Protocol per concern or special-casing one
    step's signature: "a future side-effecting step reads the same
    ``context.dry_run`` field, it doesn't get a new parameter."
    """

    job_id: str
    dry_run: bool = False


class Step(Protocol[InputT, OutputT]):
    """A single pipeline stage — macro or sub-step alike; the registry
    (``review/pipeline.py``) treats both uniformly.

    ``name`` must be unique within whichever registry a step is registered
    in — the registry's own construction fails fast on a duplicate.
    ``depends_on`` names zero or more other steps *registered in the same
    registry* whose output this step consumes as its own ``input``; the
    registry resolves execution order from these declarations and runs any
    set of steps whose dependencies are already satisfied concurrently
    (US-F2). Every concrete step this plan actually builds declares zero or
    one dependency (a linear macro chain; independent, dependency-free
    analyzer sub-steps) — the registry's dependency *resolution* supports
    an arbitrary DAG, but resolving more than one declared dependency into
    a single typed ``input`` has no concrete need yet and is rejected at
    registry-construction time (see ``review.pipeline.StepRegistry``'s own
    docstring), per architecture.md's "no interface/abstraction without a
    second real implementation already in view" rule.
    """

    name: str
    depends_on: tuple[str, ...]

    async def run(self, input: InputT, context: StepContext) -> OutputT: ...
