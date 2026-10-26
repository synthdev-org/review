"""findings.redact — the ``TextRedactor`` Protocol every static-analysis
and LLM-sourced ``Finding``'s free-text field is passed through before this
platform logs or posts it, per the plan's Phase 5 section
("Static-analysis-wide secret redaction, applied to both analyzers").

**Why this exists, beyond Gitleaks' own ``--redact`` flag:** Gitleaks'
``--redact`` (``adapter/analyzers/gitleaks.py``) only covers Gitleaks' own
JSON output. Semgrep's match messages routinely quote the matched source
line verbatim, which can itself contain a live secret incidental to the
flagged pattern — a gap only closed by redacting *every* producer's own
free-text output, uniformly, regardless of source tool.

**Placement, per docs/adr/0006-protocol-defined-at-data-model.md:**
``TextRedactor`` produces no data model of its own, but substantively
operates on ``findings.models.Finding``'s free-text fields, and has two real
step-module consumers (``review/static_analysis.py``, this phase;
``review/review_generation.py``, Phase 6) — under ADR-0006's rule, it's defined
here (the data's own package) rather than arbitrarily inside one of its two
consumers, which would also make one step module import another (forbidden
by ``review/test_step_boundaries.py``, per US-F1).

**Scope of the guarantee, stated precisely rather than left implicit:**
this redacts secrets *detectable by the vendored Gitleaks ruleset* — a
regex/entropy-based scanner, not an exhaustive one. A secret in a format
outside that ruleset's coverage (e.g. a bespoke internal token shape) can
still pass through unredacted; "redacted" means "matched by Gitleaks' own
rules," not "no secret of any kind survives."

Concrete implementation: ``adapter/analyzers/gitleaks.py``'s
``GitleaksTextRedactor`` — reuses the same vendored Gitleaks binary this
phase already vendors for scanning, invoked a second time in ``--no-git``
directory mode against a scratch file holding just the candidate finding
text, itself with ``--redact`` set so the raw candidate secret never
transits through this second invocation's own output either. A redaction
call's own failure (subprocess error, timeout, unparseable output) is
**fatal** for whichever macro step called it — posting an unredacted
finding is a materially worse outcome than failing the one job affected.

``NoOpTextRedactor`` below is the composition root's own wiring choice when
an operator has set ``secret_redaction_enabled=False`` — a deliberate,
zero-infra-import identity implementation living in this domain package
(not an adapter) because it performs no I/O of any kind and needs no
concrete infra to construct, unlike every other concrete Protocol
implementation this plan builds. Domain packages carry zero infra imports,
not zero concrete classes — a trivial, I/O-free identity implementation
doesn't violate that rule the way a real adapter would.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from findings.models import Finding

logger = logging.getLogger(__name__)

# Bounds how many concurrent ``redactor.redact`` calls ``redact_findings``
# fans out at once. Each call spawns a real OS subprocess
# (``adapter/analyzers/gitleaks.py``'s ``GitleaksTextRedactor``), and
# ``apply_redaction_ceiling`` above allows up to 50 findings through per job
# (``review/static_analysis.py``'s ceiling) — fanning all of those out with
# no cap would let one job spawn up to 50 concurrent subprocesses on a single
# Worker pod. Mirrors ``adapter/github/diff.py``'s own ``Semaphore(10)``
# bound on its concurrent blob-fetch fan-out, for the same reason.
_MAX_CONCURRENT_REDACTIONS = 10
# A path whose name held a secret is shown as ``REDACTED_PATH_PREFIX + n + "]"``.
REDACTED_PATH_PREFIX = "[REDACTED-PATH-"


def apply_redaction_ceiling(findings: list[Finding], ceiling: int) -> tuple[list[Finding], int]:
    """Cap ``findings`` at ``ceiling`` entries, returning ``(kept, dropped)``.

    Shared by ``review/static_analysis.py`` (ceiling 50) and
    ``review/review_generation.py`` (ceiling 20) — both steps redact every
    surviving finding's own message via a ``TextRedactor`` call, so each
    caps its own finding count first to bound how many redaction calls (one
    subprocess spawn each, per ``adapter/analyzers/gitleaks.py``'s
    ``GitleaksTextRedactor``) a single job can trigger. Previously
    duplicated byte-for-byte in both step modules, differing only in which
    ceiling each closed over — extracted here (this package already being
    both steps' shared, zero-infra-import dependency for ``TextRedactor``
    itself) so a future fix to this logic doesn't have to be applied twice.
    """
    if len(findings) <= ceiling:
        return findings, 0
    dropped = len(findings) - ceiling
    return findings[:ceiling], dropped


async def redact_findings(
    findings: list[Finding],
    redactor: TextRedactor,
    *,
    job_id: object,
    log_prefix: str,
) -> list[Finding]:
    """Redact every ``finding.message`` via ``redactor``, fanned out
    concurrently, with the shared logging shape both
    ``review/static_analysis.py``'s and ``review/review_generation.py``'s own
    ``_redact_all`` used to duplicate byte-for-byte (see each module's own
    ``_redact_all`` docstring for why redaction is fatal on failure and why
    it's fanned out via ``asyncio.gather`` rather than awaited in a loop).
    ``log_prefix`` (e.g. ``"static-analysis"``, ``"llm-review"``) is the
    only piece each caller varies — it appears in both the failure and
    completion log messages/extras, keeping each step's own log lines
    distinguishable in aggregate logs.
    """
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REDACTIONS)

    async def _redact_one(finding: Finding) -> str:
        async with semaphore:
            return await redactor.redact(finding.message)

    # Fanned out as real Tasks (not bare coroutines) so that a failure in one
    # can cancel the rest — mirrors adapter/github/diff.py's fetch_content
    # fan-out. Without this, a mid-fan-out exception still propagates via
    # gather immediately, but the other already-scheduled Gitleaks-backed
    # redaction subprocesses keep running detached from this job's own
    # lifecycle/timeout instead of being torn down with it.
    tasks = [asyncio.ensure_future(_redact_one(finding)) for finding in findings]
    try:
        redacted_messages = await asyncio.gather(*tasks)
    except Exception as exc:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.error(
            f"{log_prefix} redaction failed",
            extra={"job_id": job_id, "redaction_attempts": len(findings), "error": str(exc)},
        )
        raise

    redacted = [
        finding.model_copy(update={"message": redacted_message})
        for finding, redacted_message in zip(findings, redacted_messages, strict=True)
    ]
    redaction_hits = sum(
        1
        for finding, redacted_message in zip(findings, redacted_messages, strict=True)
        if redacted_message != finding.message
    )

    logger.info(
        f"{log_prefix} redaction completed",
        extra={
            "job_id": job_id,
            "redaction_attempts": len(findings),
            "redaction_hits": redaction_hits,
        },
    )
    return redacted


class TextRedactor(Protocol):
    """Redacts any text detectable by the vendored Gitleaks ruleset from
    ``text``, returning the redacted string. See this module's own
    docstring for the scope of that guarantee.
    """

    async def redact(self, text: str) -> str: ...


class NoOpTextRedactor:
    """The composition root's own wiring choice when
    ``secret_redaction_enabled=False`` — returns ``text`` unchanged. See
    this module's own docstring for why this trivial implementation lives
    here rather than in an adapter package. ``apps/worker/main.py`` pairs
    this with its own one-time ``warning``-level startup log stating
    redaction is disabled, so the reduced protection is visible, not
    silent.
    """

    async def redact(self, text: str) -> str:
        return text


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the Protocol it's built to implement.
_: type[TextRedactor] = NoOpTextRedactor
