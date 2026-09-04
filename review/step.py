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

from typing import Protocol, TypeVar

from pydantic import BaseModel

InputT = TypeVar("InputT", contravariant=True)
OutputT = TypeVar("OutputT", covariant=True)


class JobIdentity(BaseModel):
    """The six job-identifying fields (which org/repo/installation/PR a job
    is for) that every macro step's own Input and Output model carries
    forward, per ``review.pipeline.StepRegistry``'s single-dependency-input
    design: each step's ``Output`` becomes the next step's ``Input``, so
    the identity has to ride along on every one of them, unchanged, all
    the way to the final findings-output step — see
    ``review/prefilter.py``'s ``PrefilterOutput``,
    ``review/static_analysis.py``'s ``StaticAnalysisOutput``, and
    ``review/llm_review.py``'s ``LLMReviewOutput`` docstrings for why.

    A concrete step's own Input/Output model inherits this instead of
    repeating the same six fields six times across three files — previously
    duplicated byte-for-byte in ``review/prefilter.py``,
    ``review/static_analysis.py``, and ``review/llm_review.py``. Living in
    ``review/step.py`` (a framework module every step module already
    imports ``Step``/``StepContext`` from, and explicitly exempt from
    ``review/test_step_boundaries.py``'s step-to-step import-boundary
    check) keeps this a framework-level dependency, not a step-to-step one.
    """

    org_id: str
    repo_id: str
    installation_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str


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
