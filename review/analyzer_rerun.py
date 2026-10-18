"""review.analyzer_rerun — re-runs one registered analyzer over a file before
and after a candidate fix (docs/specs/spec-auto-fix.md US-AF6,
docs/plans/plan-phase-11.md Assumption 3). A helper of the ``fix_generation``
step with no ``Step`` of its own; it reuses ``static_analysis.AnalyzerSubStep``
for routing, config filtering, the per-analyzer timeout and the duration
histogram, and takes the Worker-wide analyzer slot for every run.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from enum import StrEnum

from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.static_analysis import (
    MAX_CONFIG_FILE_BYTES,
    MAX_CONFIG_FILES,
    AnalyzerRegistration,
    AnalyzerSubStep,
    RepoConfigFetcher,
    _AnalyzerSubStepInput,
)
from review.step import StepContext

logger = logging.getLogger(__name__)

# Entries are one analyzer pass or one config fetch per (repo, sha, ...); a bound
# keeps the long-lived Worker's memory flat. shortcut: oldest-first eviction, not LRU.
_MAX_CACHE_ENTRIES = 512


class ReanalysisResult(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"  # the analyzer ran fine; the fix did not clear the finding cleanly
    FAILED = "failed"  # the analyzer errored, timed out or is unknown


class AnalyzerRerunner:
    def __init__(
        self,
        registrations: Sequence[AnalyzerRegistration],
        analyzer_slots: asyncio.Semaphore,
        timeouts: Mapping[str, float],
        config_fetcher: RepoConfigFetcher | None = None,
    ) -> None:
        default_timeout = timeouts.get("static_analysis_analyzer")
        self._steps: dict[str, AnalyzerSubStep] = {}
        self._timeouts: dict[str, float] = {}
        for registration in registrations:
            timeout = timeouts.get(registration.name, default_timeout)
            if timeout is None:
                raise ValueError(f"no timeout configured for analyzer {registration.name!r}")
            self._timeouts[registration.name] = timeout
            # Whole-file comparison: a fix shifts the lines below it (Assumption 3).
            whole_file = AnalyzerRegistration(
                registration.name,
                registration.analyzer,
                matches=registration.matches,
                changed_lines_only=False,
                security_class=registration.security_class,
                config_filenames=registration.config_filenames,
            )
            self._steps[registration.name] = AnalyzerSubStep(whole_file, timeouts=self._timeouts)
        self._slots = analyzer_slots
        self._config_fetcher = config_fetcher
        self._config_names = {r.name: r.config_filenames for r in registrations}
        self._before: OrderedDict[tuple[str, str, str, str], asyncio.Future[list[Finding]]] = (
            OrderedDict()
        )
        self._configs: OrderedDict[tuple[str, str, str], dict[str, bytes]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def rerun(
        self,
        analyzer_name: str,
        path: str,
        original: bytes,
        patched: bytes,
        *,
        rule_id: str,
        patched_span: tuple[int, int],
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
    ) -> ReanalysisResult:
        """PASSED when, over the whole file: the count of ``rule_id`` findings
        drops, no rule's count rises, and no ``rule_id`` finding sits on the
        patched span's (post-patch) lines. Never raises past this boundary."""
        step = self._steps.get(analyzer_name)
        if step is None:
            return ReanalysisResult.FAILED
        try:
            config = await self._config(analyzer_name, installation_id, repo_full_name, head_sha)
            key = (repo_full_name, head_sha, path, analyzer_name)
            before = await self._original_findings(key, step, original, config)
            after = await self._run(step, path, patched, config)
        except Exception as exc:
            logger.warning(
                "fix re-analysis failed",
                extra={"analyzer_name": analyzer_name, "error": f"{type(exc).__name__}: {exc}"},
            )
            return ReanalysisResult.FAILED

        before_counts = Counter(f.rule_id for f in before)
        after_counts = Counter(f.rule_id for f in after)
        start, end = patched_span
        cleared = after_counts[rule_id] < before_counts[rule_id]
        regressed = any(after_counts[rule] > count for rule, count in before_counts.items()) or any(
            rule not in before_counts for rule in after_counts
        )
        on_patch = any(
            f.rule_id == rule_id and f.line is not None and start <= f.line <= end for f in after
        )
        if cleared and not regressed and not on_patch:
            return ReanalysisResult.PASSED
        return ReanalysisResult.REJECTED

    async def _original_findings(
        self,
        key: tuple[str, str, str, str],
        step: AnalyzerSubStep,
        original: bytes,
        config: dict[str, bytes],
    ) -> list[Finding]:
        """The "before" run, executed once per (job, file, analyzer) even when
        several fixes in the file ask concurrently; a failed run is not cached."""
        pending = self._before.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        pending = asyncio.get_running_loop().create_future()
        pending.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark retrieved
        self._remember(self._before, key, pending)
        try:
            findings = await self._run(step, key[2], original, config)
        except BaseException as exc:
            self._before.pop(key, None)
            if isinstance(exc, Exception):
                pending.set_exception(exc)
            else:
                pending.cancel()
            raise
        pending.set_result(findings)
        return findings

    async def _run(
        self, step: AnalyzerSubStep, path: str, data: bytes, config: dict[str, bytes]
    ) -> list[Finding]:
        diff = Diff(files=(DiffFile(path=path, status="modified", mode="100644"),))
        sub_input = _AnalyzerSubStepInput(diff=diff, content={path: data}, config_files=config)
        # The slot is taken before the timed region, so queueing never eats the timeout.
        async with self._slots:
            return await asyncio.wait_for(
                step.run(sub_input, StepContext(job_id="fix_generation")),
                timeout=self._timeouts[step.name],
            )

    async def _config(
        self, analyzer_name: str, installation_id: int, repo_full_name: str, head_sha: str
    ) -> dict[str, bytes]:
        """Repo-native config for one engine, fetched once per job; any failure
        degrades to the default profile, as in ``StaticAnalysisStep``."""
        names = self._config_names[analyzer_name]
        if self._config_fetcher is None or not names:
            return {}
        key = (repo_full_name, head_sha, analyzer_name)
        async with self._lock:
            cached = self._configs.get(key)
            if cached is not None:
                return cached
            try:
                files = await self._config_fetcher.fetch_config_files(
                    installation_id=installation_id,
                    repo_full_name=repo_full_name,
                    commit_sha=head_sha,
                    names=names,
                    max_file_bytes=MAX_CONFIG_FILE_BYTES,
                    max_files=MAX_CONFIG_FILES,
                )
            except Exception as exc:
                logger.warning(
                    "analyzer repo config fetch failed; using default profile",
                    extra={"analyzer_name": analyzer_name, "error": type(exc).__name__},
                )
                files = {}
            self._remember(self._configs, key, files)
            return files

    @staticmethod
    def _remember[K, V](cache: OrderedDict[K, V], key: K, value: V) -> None:
        cache[key] = value
        while len(cache) > _MAX_CACHE_ENTRIES:
            cache.popitem(last=False)
