"""adapter.analyzers.trivy — the concrete ``StaticAnalyzer`` backed by the
pinned ``trivy`` binary (``trivy`` on ``PATH``; the Dockerfile's ``trivy`` stage
installs it with a checksum-verified version, ci.yml mirrors that), per
docs/plans/plan-phase-9.md Phase 6. ``trivy config`` only: IaC
misconfiguration for Terraform, Dockerfiles and plain Kubernetes YAML. Helm
and CloudFormation are out of scope (``match_iac`` never routes them and
``--misconfig-scanners`` pins the three scanners regardless).

**Offline.** The misconfiguration checks are embedded in the binary;
``--skip-check-update`` stops the bundle download, ``--disable-telemetry`` and
``--skip-version-check`` stop the phone-home calls, and the cache lives inside
the per-invocation scratch directory. Trivy has no switch for Terraform remote
module download, so the child also gets a dead HTTP(S) proxy and git is
disabled: a ``module`` block with a registry source fails fast instead of
fetching (it is logged by Trivy and the rest of the scan is unaffected).
Go never proxies loopback, so a ``module`` source of ``127.0.0.1:port/...``
would still connect: before materialization every non-relative ``module``
``source`` in a ``.tf`` file is rewritten to a local placeholder (same line
count, so finding lines are unchanged).
shortcut: a brace/string/comment scanner, not an HCL parser (heredocs are not
understood); the operator-owned network policy (ADR 0013) is the real
boundary.

**Config is data, never code.** ``classify_trivy_config`` (pure,
unit-tested) honors the repo-root ``trivy.yaml`` unless it names custom Rego
checks/data, a remote checks bundle, or any option that reads or writes a path
outside the fetched set (the skip is logged with ``config_skip_reason``); the
pinned ``trivy_default.yaml`` is used otherwise. A ``.trivyignore`` /
``.trivyignore.yaml`` is honored as data. Only honored files are written to the
scan directory, and ``--config`` / ``--ignorefile`` are always passed so Trivy
never auto-loads an unvetted file by name. A config Trivy itself rejects is
re-run once with the default profile.

**Line mapping (US-A3's Trivy rule).** A misconfiguration belongs to a
resource, so it is kept when the resource's reported ``StartLine``..``EndLine``
overlaps at least one added/modified line of the file's patch, and is posted on
the first such line (GitHub only accepts comments on diff lines). A finding with
no line range (whole-file checks such as Dockerfile's missing ``USER``) is kept
when the file has any added/modified line, anchored on its first. Everything
else is dropped and counted in the ``filtered_count`` log field. This is why
the registration is not ``changed_lines_only``.

**Exit codes:** ``--exit-code 0`` makes "misconfigurations found" a success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from adapter.analyzers.exceptions import AnalyzerInvocationError, AnalyzerOutputParseError
from adapter.analyzers.subprocess_runner import (
    cleanup_temp_directory,
    materialize_into_temp_directory,
    original_path_mapping,
    run_analyzer_subprocess,
)
from findings.models import Finding, Severity
from ingestion.diff import Diff
from review.changed_lines import changed_lines
from review.static_analysis import ConfigOutcome, StaticAnalyzer

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_PATH = Path(__file__).parent / "trivy_default.yaml"

TRIVY_CONFIG_FILENAMES = ("trivy.yaml", ".trivyignore", ".trivyignore.yaml")
_CONFIG_NAME = "trivy.yaml"
_IGNORE_FILENAMES = (".trivyignore", ".trivyignore.yaml")

# Option path -> skip reason. Present with a truthy value => the whole config is
# not honored. Everything not listed is either harmless for `trivy config` or is
# overridden on the command line (format, exit code, cache dir, ignorefile,
# scanners).
_FORBIDDEN_OPTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rego", "check"), "custom_checks_not_allowed"),
    (("rego", "data"), "custom_checks_not_allowed"),
    (("misconfiguration", "config-file-schemas"), "custom_checks_not_allowed"),
    (("misconfiguration", "checks-bundle-repository"), "remote_checks_not_allowed"),
    (("misconfiguration", "terraform", "vars"), "path_option_not_allowed"),
    (("misconfiguration", "cloudformation", "params"), "path_option_not_allowed"),
    (("misconfiguration", "helm", "values"), "path_option_not_allowed"),
    (("misconfiguration", "helm", "set-file"), "path_option_not_allowed"),
    (("misconfiguration", "ansible"), "path_option_not_allowed"),
    (("module", "dir"), "path_option_not_allowed"),
    (("module", "enable-modules"), "path_option_not_allowed"),
    (("output",), "path_option_not_allowed"),
    (("output-plugin-arg",), "path_option_not_allowed"),
    (("ignore-policy",), "path_option_not_allowed"),
    (("cacert",), "path_option_not_allowed"),
)

_SEVERITY_MAP: dict[str, Severity] = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
}
_DEFAULT_SEVERITY: Severity = "medium"

# Trivy fetches Terraform registry modules over HTTPS and git sources via git;
# both are pointed nowhere (see the module docstring).
_OFFLINE_ENV = {
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "NO_PROXY": "",
    "no_proxy": "",
    "GIT_ALLOW_PROTOCOL": "none",
}


_PLACEHOLDER = "./.review-remote-module-disabled"
_MODULE_HEAD = re.compile(r'\bmodule\s+"[^"]*"\s*\{')
# Only `./...` without a `..` segment stays: `../x` would let Trivy read .tf outside the scan dir.
_SOURCE_ATTR = re.compile(r'(?<![\w-])(source\s*=\s*)"(?!\./(?:(?!\.\.)[^"])*")[^"]*"')


def _rewrite_tf_module_sources(text: str) -> str:
    """Rewrite ``source`` at the top level of each ``module`` block, skipping
    strings and comments so nested ``{...}`` (for_each, providers) cannot hide it."""
    out: list[str] = []
    copied = search = 0
    n = len(text)
    while head := _MODULE_HEAD.search(text, search):
        i = head.end()
        depth = 1
        while i < n and depth:
            c = text[i]
            if c == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif c == "#" or text.startswith("//", i):
                end = text.find("\n", i)
                i = n if end < 0 else end - 1
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif depth == 1 and (m := _SOURCE_ATTR.match(text, i)):
                out.append(text[copied:i] + f'{m.group(1)}"{_PLACEHOLDER}"')
                copied = i = m.end()
                continue
            i += 1
        search = i
    out.append(text[copied:])
    return "".join(out)


def _rewrite_tf_json_module_sources(raw: bytes) -> bytes:
    try:
        document = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(document, dict):
        return raw
    modules = document.get("module")
    changed = False
    for block in modules if isinstance(modules, list) else [modules]:
        if isinstance(block, dict):
            for body in block.values():
                for entry in body if isinstance(body, list) else [body]:
                    source = entry.get("source") if isinstance(entry, dict) else None
                    if isinstance(source, str) and not (
                        source.startswith("./") and ".." not in source
                    ):
                        entry["source"] = _PLACEHOLDER
                        changed = True
    # Re-serializing collapses the file to one line, which would break the
    # line-range mapping; only do it when a source was actually rewritten.
    return json.dumps(document).encode() if changed else raw


def _neutralize_remote_modules(content: Mapping[str, bytes]) -> dict[str, bytes]:
    """Point every non-relative Terraform module source at a local placeholder."""
    result: dict[str, bytes] = {}
    for path, raw in content.items():
        if path.endswith(".tf"):
            raw = _rewrite_tf_module_sources(raw.decode("utf-8", "replace")).encode()
        elif path.endswith(".tf.json"):
            raw = _rewrite_tf_json_module_sources(raw)
        result[path] = raw
    return result


@dataclass(frozen=True)
class TrivyConfigChoice:
    """``config_name`` is the fetched ``trivy.yaml`` to pass to ``--config``, or
    ``None`` for the pinned default profile; ``ignore_name`` the honored ignore
    file, if any."""

    config_name: str | None
    ignore_name: str | None
    outcome: ConfigOutcome
    skipped_path: str | None = None


def _lookup(document: object, path: tuple[str, ...]) -> object:
    node: object = document
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _lower_keys(node: object, memo: dict[int, object]) -> object:
    """Trivy reads ``trivy.yaml`` through viper, which is case-insensitive:
    ``Output:`` is ``output:``. Memoized by identity so a YAML alias DAG is
    walked once, not expanded as a tree."""
    if id(node) in memo:
        return memo[id(node)]
    if isinstance(node, dict):
        lowered: dict[object, object] = {}
        memo[id(node)] = lowered
        for k, v in node.items():
            lowered[k.lower() if isinstance(k, str) else k] = _lower_keys(v, memo)
        return lowered
    if isinstance(node, list):
        items: list[object] = []
        memo[id(node)] = items
        items.extend(_lower_keys(v, memo) for v in node)
        return items
    return node


def _config_reason(raw: bytes) -> str | None:
    """Why ``trivy.yaml`` cannot be honored, or ``None`` when it is plain data."""
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return "config_invalid"
    if document is None:
        return None
    if not isinstance(document, dict):
        return "config_invalid"
    document = _lower_keys(document, {})
    for path, reason in _FORBIDDEN_OPTIONS:
        if _lookup(document, path):
            return reason
    backend = _lookup(document, ("cache", "backend"))
    if backend not in (None, "", "memory"):
        return "path_option_not_allowed"
    return None


def _ignore_file_valid(name: str, raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8")
        if name.endswith(".yaml"):
            return isinstance(yaml.safe_load(text), dict | None)
    except (UnicodeDecodeError, yaml.YAMLError):
        return False
    return True


def classify_trivy_config(config_files: Mapping[str, bytes]) -> TrivyConfigChoice:
    """``_classify_trivy_config`` that treats a pathological config (nesting deep
    enough to overflow the YAML parser's stack) as invalid, never as a crash."""
    try:
        return _classify_trivy_config(config_files)
    except (RecursionError, TypeError):
        return TrivyConfigChoice(None, None, ConfigOutcome("default", "config_invalid"))


def _classify_trivy_config(config_files: Mapping[str, bytes]) -> TrivyConfigChoice:
    """Pure data-only classifier (no I/O): which fetched ``trivy.yaml`` and
    ignore file Trivy may be pointed at, with a ``config_skip_reason`` when
    something was refused."""
    config_name: str | None = None
    outcome = ConfigOutcome("default")
    skipped_path: str | None = None
    if _CONFIG_NAME in config_files:
        reason = _config_reason(config_files[_CONFIG_NAME])
        if reason is None:
            config_name, outcome = _CONFIG_NAME, ConfigOutcome("repo")
        else:
            outcome, skipped_path = ConfigOutcome("default", reason), _CONFIG_NAME

    ignore_name = next((n for n in _IGNORE_FILENAMES if n in config_files), None)
    if ignore_name is not None and not _ignore_file_valid(ignore_name, config_files[ignore_name]):
        outcome = ConfigOutcome(outcome.source, outcome.skip_reason or "ignorefile_invalid")
        skipped_path = skipped_path or ignore_name
        ignore_name = None
    if ignore_name is not None and outcome.source == "default":
        outcome = ConfigOutcome("repo")
    return TrivyConfigChoice(config_name, ignore_name, outcome, skipped_path)


def _argv(config_name: str, ignore_name: str, cache_dir: str) -> list[str]:
    return [
        "trivy",
        "config",
        # Explicit, so Trivy never auto-loads a trivy.yaml / .trivyignore by name.
        "--config",
        config_name,
        "--ignorefile",
        ignore_name,
        "--format",
        "json",
        # "Misconfigurations found" is success; only a real failure exits non-zero.
        "--exit-code",
        "0",
        # Spec scope: Terraform, Dockerfile, plain Kubernetes. Never Helm/CloudFormation.
        "--misconfig-scanners",
        "terraform,dockerfile,kubernetes",
        # Embedded checks only; no bundle download, telemetry or version check.
        "--skip-check-update",
        "--disable-telemetry",
        "--skip-version-check",
        # Nothing but the scratch directory is ever written.
        "--cache-dir",
        cache_dir,
        # Keep log lines off the merged stdout+stderr stream we parse as JSON.
        "--quiet",
        ".",
    ]


def _parse_output(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="trivy", detail=str(exc)) from exc
    if not isinstance(parsed, dict):
        raise AnalyzerOutputParseError(tool="trivy", detail="output was not a JSON object")
    results = parsed.get("Results") or []
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise AnalyzerOutputParseError(tool="trivy", detail="malformed Results")
    return results


def _anchor_line(start: object, end: object, changed: frozenset[int]) -> int | None:
    """The added/modified line to post on, or ``None`` to drop the finding
    (the resource-range rule in the module docstring)."""
    if not changed:
        return None
    if not isinstance(start, int) or start < 1:
        return min(changed)  # whole-file finding
    last = end if isinstance(end, int) and end >= start else start
    return min((line for line in changed if start <= line <= last), default=None)


def _message(entry: dict[str, Any]) -> str:
    title, message = entry.get("Title"), entry.get("Message")
    parts = [p for p in (title, message) if isinstance(p, str) and p]
    return ": ".join(dict.fromkeys(parts)) or "Misconfiguration"


def _to_findings(
    results: list[dict[str, Any]],
    original_by_normalized: dict[str, str],
    patches: Mapping[str, str | None],
) -> tuple[list[Finding], int]:
    """Normalize ``Results`` to findings anchored on changed lines; returns
    ``(findings, filtered_count)``."""
    findings: list[Finding] = []
    filtered = 0
    cache: dict[str, frozenset[int]] = {}
    for result in results:
        entries = result.get("Misconfigurations") or []
        target = result.get("Target")
        if not entries:
            continue
        if not isinstance(target, str) or not isinstance(entries, list):
            raise AnalyzerOutputParseError(tool="trivy", detail="malformed result")
        relative = posixpath.normpath(target)
        if posixpath.isabs(relative) or relative == ".." or relative.startswith("../"):
            # A local Terraform module source outside the scan directory: not the
            # PR's file, so drop that Result rather than losing the whole run.
            # shortcut: Trivy still reads the module's .tf; only reporting is
            # contained. Upgrade by sandboxing the filesystem (worker pod policy).
            logger.warning("trivy result outside scan directory dropped")
            continue
        original = original_by_normalized.get(relative, relative)
        if original not in cache:
            cache[original] = changed_lines(patches.get(original))
        for entry in entries:
            if not isinstance(entry, dict):
                raise AnalyzerOutputParseError(tool="trivy", detail="malformed entry")
            cause = entry.get("CauseMetadata")
            cause = cause if isinstance(cause, dict) else {}
            line = _anchor_line(cause.get("StartLine"), cause.get("EndLine"), cache[original])
            if line is None:
                filtered += 1
                continue
            rule_id = entry.get("ID")
            raw_severity = entry.get("Severity")
            findings.append(
                Finding(
                    file=original,
                    line=line,
                    severity=_SEVERITY_MAP.get(str(raw_severity), _DEFAULT_SEVERITY),
                    message=_message(entry),
                    source="trivy",
                    rule_id=rule_id if isinstance(rule_id, str) else None,
                )
            )
    return findings, filtered


class TrivyAnalyzer:
    """``review.static_analysis.StaticAnalyzer`` backed by the ``trivy`` binary.
    See this module's docstring."""

    name = "trivy"

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        if not content:
            return []
        choice = classify_trivy_config(config_files)
        logger.info(
            "trivy config selected",
            extra={
                "analyzer_name": self.name,
                **choice.outcome.log_fields(),
                **({"config_path": choice.skipped_path} if choice.skipped_path else {}),
            },
        )
        choice.outcome.record(self.name)
        honored = {
            name: config_files[name] for name in (choice.config_name, choice.ignore_name) if name
        }
        directory = await materialize_into_temp_directory(
            {**honored, **_neutralize_remote_modules(content)}
        )
        try:
            suffix = uuid.uuid4().hex
            default_name = f".review-trivy-default-{suffix}.yaml"
            await asyncio.to_thread(
                shutil.copyfile, _DEFAULT_PROFILE_PATH, directory / default_name
            )
            cache_dir = str(directory / f".review-trivy-cache-{suffix}")
            ignore_name = choice.ignore_name or ""
            original_by_normalized = original_path_mapping(content)
            started = asyncio.get_running_loop().time()

            async def scan(config_name: str, budget: float) -> bytes:
                return await run_analyzer_subprocess(
                    _argv(config_name, ignore_name, cache_dir),
                    cwd=directory,
                    timeout=budget,
                    env_overrides=_OFFLINE_ENV,
                )

            try:
                output = await scan(choice.config_name or default_name, timeout)
            except AnalyzerInvocationError:
                remaining = timeout - (asyncio.get_running_loop().time() - started)
                if choice.config_name is None or remaining <= 0:
                    raise
                logger.warning(
                    "trivy rejected the repo config; re-running with the default profile",
                    extra={
                        "analyzer_name": self.name,
                        "config_source": "default",
                        "config_skip_reason": "rejected_by_tool",
                        "config_path": choice.config_name,
                    },
                )
                ConfigOutcome("default", "rejected_by_tool").record(self.name)
                output = await scan(default_name, remaining)
            findings, filtered = _to_findings(
                _parse_output(output),
                original_by_normalized,
                {f.path: f.patch for f in diff.files},
            )
            logger.info(
                "trivy findings mapped to changed resource ranges",
                extra={"analyzer_name": self.name, "filtered_count": filtered},
            )
            return findings
        finally:
            await cleanup_temp_directory(directory)


_: type[StaticAnalyzer] = TrivyAnalyzer
