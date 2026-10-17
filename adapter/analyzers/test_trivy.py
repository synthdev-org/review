"""Tests for adapter.analyzers.trivy (plan-phase-9 Phase 6): the pure data-only
config classifier, resource-range line mapping and output normalization at the
unit tier, and the real pinned ``trivy`` binary on Terraform / Dockerfile /
Kubernetes fixtures at the integration tier (US-A1..A6, A9 for IaC).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.trivy import (
    _DEFAULT_PROFILE_PATH,
    _OFFLINE_ENV,
    TRIVY_CONFIG_FILENAMES,
    TrivyAnalyzer,
    TrivyConfigChoice,
    _anchor_line,
    _neutralize_remote_modules,
    _parse_output,
    _to_findings,
    classify_trivy_config,
)
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.file_matchers import match_iac
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep
from review.step import StepContext

_BUCKET_ACL = 'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n  acl    = "public-read"\n}\n'
_OPEN_INGRESS = (
    'resource "aws_security_group_rule" "r" {\n'
    '  type              = "ingress"\n'
    '  cidr_blocks       = ["0.0.0.0/0"]\n'
    "  from_port         = 0\n"
    "  to_port           = 65535\n"
    '  protocol          = "tcp"\n'
    '  security_group_id = "sg-1"\n'
    '  description       = "open"\n'
    "}\n"
)
_TERRAFORM = _BUCKET_ACL + "\n" + _OPEN_INGRESS
_DOCKERFILE_ROOT = "FROM ubuntu:22.04\nRUN apt-get update && apt-get install -y curl\n"
_DOCKERFILE_CLEAN = (
    "FROM ubuntu:22.04\n"
    "RUN apt-get update && apt-get install -y --no-install-recommends curl \\\n"
    "    && rm -rf /var/lib/apt/lists/*\n"
    "HEALTHCHECK CMD curl -f http://localhost/ || exit 1\n"
    "USER 1000\n"
)
# Only LOW/MEDIUM-class: no HEALTHCHECK and a non-pinned tag, but a non-root USER.
_DOCKERFILE_LOW_ONLY = "FROM ubuntu:22.04\nUSER 1000\n"
_PRIVILEGED_POD = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "metadata:\n"
    "  name: p\n"
    "spec:\n"
    "  containers:\n"
    "    - name: c\n"
    "      image: nginx\n"
    "      securityContext:\n"
    "        privileged: true\n"
)
_CLEAN_POD = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "metadata:\n"
    "  name: p\n"
    "spec:\n"
    "  automountServiceAccountToken: false\n"
    "  securityContext:\n"
    "    runAsNonRoot: true\n"
    "    runAsUser: 10001\n"
    "    seccompProfile:\n"
    "      type: RuntimeDefault\n"
    "  containers:\n"
    "    - name: c\n"
    "      image: nginx:1.27@sha256:" + "0" * 64 + "\n"
    "      resources:\n"
    "        limits: {cpu: 100m, memory: 64Mi}\n"
    "        requests: {cpu: 100m, memory: 64Mi}\n"
    "      securityContext:\n"
    "        allowPrivilegeEscalation: false\n"
    "        readOnlyRootFilesystem: true\n"
    "        runAsNonRoot: true\n"
    "        runAsUser: 10001\n"
    "        capabilities: {drop: [ALL]}\n"
)


@pytest.fixture(autouse=True)
def _enable_analyzer_logger() -> None:
    # An earlier test's logging setup may have disabled this already-imported logger.
    logging.getLogger("adapter.analyzers.trivy").disabled = False


def _added(path: str, text: str) -> DiffFile:
    count = len(text.splitlines())
    patch = f"@@ -0,0 +1,{count} @@\n" + "".join(f"+{line}\n" for line in text.splitlines())
    return DiffFile(path=path, status="added", patch=patch, mode="100644")


def _diff_of(content: dict[str, str]) -> Diff:
    return Diff(files=tuple(_added(path, text) for path, text in content.items()))


def _run(
    content: dict[str, str],
    config_files: dict[str, bytes] | None = None,
    diff: Diff | None = None,
) -> Coroutine[Any, Any, list[Finding]]:
    return TrivyAnalyzer().run(
        diff or _diff_of(content),
        {path: text.encode() for path, text in content.items()},
        timeout=60,
        config_files=config_files or {},
    )


# --- unit: pinned default profile ------------------------------------------------


def test_default_profile_checksum_matches() -> None:
    actual = hashlib.sha256(_DEFAULT_PROFILE_PATH.read_bytes()).hexdigest()
    pinned = _DEFAULT_PROFILE_PATH.with_suffix(".yaml.sha256").read_text().strip()
    assert actual == pinned, (
        "trivy_default.yaml changed without trivy_default.yaml.sha256 being re-pinned"
    )


def test_offline_environment_points_every_http_route_at_a_dead_proxy_and_disables_git() -> None:
    assert _OFFLINE_ENV["HTTPS_PROXY"] == _OFFLINE_ENV["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert _OFFLINE_ENV["NO_PROXY"] == ""
    assert _OFFLINE_ENV["GIT_ALLOW_PROTOCOL"] == "none"


# --- unit: data-only classifier (US-A4) ------------------------------------------


def _classify(files: dict[str, str]) -> TrivyConfigChoice:
    return classify_trivy_config({name: text.encode() for name, text in files.items()})


def test_no_config_uses_the_default_profile_silently() -> None:
    choice = _classify({})
    assert (choice.config_name, choice.ignore_name) == (None, None)
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is None


def test_trivy_yaml_and_ignore_file_are_honored() -> None:
    choice = _classify({"trivy.yaml": "severity: [LOW]\n", ".trivyignore": "AWS-0086\n"})
    assert (choice.config_name, choice.ignore_name) == ("trivy.yaml", ".trivyignore")
    assert choice.outcome.source == "repo"
    assert choice.outcome.skip_reason is None


def test_ignore_file_alone_is_honored_with_the_default_profile() -> None:
    choice = _classify({".trivyignore.yaml": "misconfigurations:\n  - id: DS-0002\n"})
    assert (choice.config_name, choice.ignore_name) == (None, ".trivyignore.yaml")
    assert choice.outcome.source == "repo"


def test_trivyignore_wins_over_the_yaml_form() -> None:
    choice = _classify({".trivyignore": "X\n", ".trivyignore.yaml": "a: 1\n"})
    assert choice.ignore_name == ".trivyignore"


def test_empty_trivy_yaml_is_honored_as_no_options() -> None:
    assert _classify({"trivy.yaml": ""}).config_name == "trivy.yaml"


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ("rego:\n  check: [policies/]\n", "custom_checks_not_allowed"),
        ("rego:\n  data: [data/]\n", "custom_checks_not_allowed"),
        (
            "misconfiguration:\n  config-file-schemas: [s.json]\n",
            "custom_checks_not_allowed",
        ),
        (
            "misconfiguration:\n  checks-bundle-repository: evil.example/checks:1\n",
            "remote_checks_not_allowed",
        ),
        ("misconfiguration:\n  terraform:\n    vars: [/etc/x.tfvars]\n", "path_option_not_allowed"),
        ("misconfiguration:\n  helm:\n    values: [v.yaml]\n", "path_option_not_allowed"),
        ("module:\n  dir: /tmp/m\n", "path_option_not_allowed"),
        ("output: /tmp/out.json\n", "path_option_not_allowed"),
        ("cacert: /tmp/ca.pem\n", "path_option_not_allowed"),
        ("cache:\n  backend: redis://x:6379\n", "path_option_not_allowed"),
        ("ignore-policy: /tmp/p.rego\n", "path_option_not_allowed"),
        ("severity: [\n", "config_invalid"),
        ("- a\n- b\n", "config_invalid"),
    ],
)
def test_unsafe_config_falls_back_to_default_with_a_reason(config: str, reason: str) -> None:
    choice = _classify({"trivy.yaml": config})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == reason
    assert choice.skipped_path == "trivy.yaml"


def test_harmless_and_overridden_options_do_not_block_the_config() -> None:
    config = (
        "format: table\nexit-code: 1\nseverity: [LOW]\ncache:\n  backend: memory\n  dir: /x\n"
        "scan:\n  skip-dirs: [vendor]\nrego:\n  check: []\n"
    )
    assert _classify({"trivy.yaml": config}).config_name == "trivy.yaml"


def test_invalid_ignore_file_is_dropped_but_a_good_config_is_kept() -> None:
    choice = classify_trivy_config(
        {"trivy.yaml": b"severity: [LOW]\n", ".trivyignore": b"\xff\xfe"}
    )
    assert (choice.config_name, choice.ignore_name) == ("trivy.yaml", None)
    assert choice.outcome.skip_reason == "ignorefile_invalid"
    assert choice.skipped_path == ".trivyignore"


def test_only_the_allow_listed_names_are_ever_considered() -> None:
    assert set(TRIVY_CONFIG_FILENAMES) == {"trivy.yaml", ".trivyignore", ".trivyignore.yaml"}


# --- unit: resource-range line mapping (US-A3's Trivy rule) -----------------------


@pytest.mark.parametrize(
    ("start", "end", "changed", "expected"),
    [
        (1, 4, frozenset({3}), 3),  # added line inside the resource
        (1, 4, frozenset({4, 9}), 4),  # boundary of the range counts
        (1, 4, frozenset({5}), None),  # edit just past the resource: dropped
        (6, 14, frozenset({3, 8, 10}), 8),  # first changed line within the range
        (6, None, frozenset({6}), 6),  # no end: single-line range
        (6, 3, frozenset({6}), 6),  # inverted end: treated as single-line
        (None, None, frozenset({4, 2}), 2),  # no range: whole-file finding on first changed line
        (0, 0, frozenset({4}), 4),
        (1, 4, frozenset(), None),  # no patch / no changed lines: dropped
        (None, None, frozenset(), None),
    ],
)
def test_anchor_line(start: object, end: object, changed: frozenset[int], expected: int) -> None:
    assert _anchor_line(start, end, changed) == expected


def _result(target: str, *entries: dict[str, Any]) -> dict[str, Any]:
    return {"Target": target, "Misconfigurations": list(entries)}


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ID": "AWS-0086",
        "Title": "S3 Access block should block public ACL",
        "Message": "No public access block so not blocking public acls",
        "Severity": "HIGH",
        "CauseMetadata": {"StartLine": 1, "EndLine": 4},
    }
    return {**entry, **overrides}


def test_findings_are_normalized_and_mapped_back_to_original_paths() -> None:
    patch = "@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n"
    findings, filtered = _to_findings(
        [_result("infra/main.tf", _entry())],
        {"infra/main.tf": "orig/main.tf"},
        {"orig/main.tf": patch},
    )
    assert filtered == 0
    assert [(f.file, f.line, f.severity, f.source, f.rule_id) for f in findings] == [
        ("orig/main.tf", 1, "high", "trivy", "AWS-0086")
    ]
    assert findings[0].message == (
        "S3 Access block should block public ACL: "
        "No public access block so not blocking public acls"
    )


def test_findings_outside_added_lines_are_dropped_and_counted() -> None:
    patch = "@@ -8,1 +8,1 @@\n-x\n+y\n"
    findings, filtered = _to_findings(
        [_result("main.tf", _entry(), _entry(ID="AWS-0001"))], {}, {"main.tf": patch}
    )
    assert (findings, filtered) == ([], 2)


def test_file_without_a_patch_is_dropped_not_posted_file_wide() -> None:
    findings, filtered = _to_findings(
        [_result("Dockerfile", _entry(CauseMetadata={}))], {}, {"Dockerfile": None}
    )
    assert (findings, filtered) == ([], 1)


@pytest.mark.parametrize(
    ("raw", "severity"),
    [("CRITICAL", "critical"), ("MEDIUM", "medium"), ("LOW", "low"), ("UNKNOWN", "medium")],
)
def test_severity_table_and_unmapped_default(raw: str, severity: str) -> None:
    findings, _ = _to_findings(
        [_result("m.tf", _entry(Severity=raw))], {}, {"m.tf": "@@ -0,0 +1 @@\n+a\n"}
    )
    assert findings[0].severity == severity


def test_results_without_misconfigurations_are_skipped() -> None:
    assert _to_findings([{"Target": "x", "Class": "config"}], {}, {}) == ([], 0)


@pytest.mark.parametrize(
    "results",
    [
        [{"Target": 3, "Misconfigurations": [_entry()]}],
        [_result("m.tf", "not-a-dict")],  # type: ignore[arg-type]
    ],
)
def test_malformed_results_raise_parse_error(results: list[dict[str, Any]]) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _to_findings(results, {}, {})


@pytest.mark.parametrize(
    "raw", [b"not json", b"\xff", b"[1]", b'{"Results": 3}', b'{"Results": [1]}']
)
def test_garbage_output_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _parse_output(raw)


def test_output_without_results_is_empty() -> None:
    assert _parse_output(b'{"SchemaVersion": 2}') == []


@pytest.mark.asyncio
async def test_empty_content_short_circuits_without_spawning_trivy() -> None:
    assert await TrivyAnalyzer().run(Diff(files=()), {}, timeout=1, config_files={}) == []


# --- integration: real pinned binary --------------------------------------------


def _rules(findings: Sequence[Finding]) -> set[tuple[str, str | None]]:
    return {(f.file, f.rule_id) for f in findings}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_terraform_dockerfile_and_kubernetes_misconfigurations_are_reported() -> None:
    findings = await _run(
        {
            "infra/main.tf": _TERRAFORM,
            "svc/Dockerfile": _DOCKERFILE_ROOT,
            "k8s/pod.yaml": _PRIVILEGED_POD,
        }
    )

    assert all(f.source == "trivy" and f.line is not None for f in findings)
    assert any(f.file == "infra/main.tf" and f.rule_id == "AWS-0086" for f in findings)
    assert any(f.file == "infra/main.tf" and f.rule_id == "AWS-0107" for f in findings)
    assert any(f.file == "svc/Dockerfile" and f.rule_id == "DS-0002" for f in findings)
    assert any(f.file == "k8s/pod.yaml" and f.severity in {"high", "critical"} for f in findings)
    assert {f.severity for f in findings} <= {"high", "critical"}  # default = HIGH/CRITICAL


@pytest.mark.asyncio
@pytest.mark.integration
async def test_clean_iac_and_low_severity_only_iac_produce_no_findings() -> None:
    assert (
        await _run(
            {
                "Dockerfile": _DOCKERFILE_CLEAN,
                "k8s/pod.yaml": _CLEAN_POD,
                "low/Dockerfile": _DOCKERFILE_LOW_ONLY,
            }
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_findings_found_is_success_not_an_invocation_error() -> None:
    # Would raise AnalyzerInvocationError if "misconfigurations found" were a failure.
    assert await _run({"Dockerfile": _DOCKERFILE_ROOT})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_only_resources_overlapping_added_lines_are_reported() -> None:
    # Line 3 (the bucket's acl) is the only modified line; the ingress rule
    # (lines 6-14) is untouched and its AWS-0107 finding must not be posted.
    patch = (
        '@@ -1,4 +1,4 @@\n resource "aws_s3_bucket" "b" {\n   bucket = "x"\n'
        '-  acl    = "private"\n+  acl    = "public-read"\n }\n'
    )
    diff = Diff(files=(DiffFile(path="main.tf", status="modified", patch=patch, mode="100644"),))

    findings = await _run({"main.tf": _TERRAFORM}, diff=diff)

    assert findings
    assert {f.line for f in findings} == {3}
    assert not any(f.rule_id == "AWS-0107" for f in findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_whole_file_finding_is_anchored_on_the_first_added_line() -> None:
    patch = "@@ -1,2 +1,3 @@\n FROM ubuntu:22.04\n+# note\n RUN true\n"
    diff = Diff(files=(DiffFile(path="Dockerfile", status="modified", patch=patch, mode="100644"),))

    findings = await _run({"Dockerfile": "FROM ubuntu:22.04\n# note\nRUN true\n"}, diff=diff)

    user = [f for f in findings if f.rule_id == "DS-0002"]  # "no USER": no line range
    assert [f.line for f in user] == [2]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_file_without_a_patch_yields_nothing() -> None:
    diff = Diff(files=(DiffFile(path="Dockerfile", status="modified", patch=None, mode="100644"),))
    assert await _run({"Dockerfile": _DOCKERFILE_ROOT}, diff=diff) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_governs_which_severities_apply() -> None:
    content = {"Dockerfile": _DOCKERFILE_LOW_ONLY}

    assert await _run(content) == []  # default profile: HIGH/CRITICAL only
    found = await _run(content, {"trivy.yaml": b"severity:\n  - LOW\n  - MEDIUM\n"})
    assert found
    assert {f.severity for f in found} <= {"low", "medium"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_ignore_file_suppresses_the_listed_check() -> None:
    content = {"Dockerfile": _DOCKERFILE_ROOT}
    assert "DS-0002" in {f.rule_id for f in await _run(content)}

    ignored = await _run(content, {".trivyignore": b"# accepted risk\nDS-0002\n"})

    assert "DS-0002" not in {f.rule_id for f in ignored}
    assert ignored  # the other findings are still reported


@pytest.mark.asyncio
@pytest.mark.integration
async def test_yaml_ignore_file_is_honored() -> None:
    config = {".trivyignore.yaml": b"misconfigurations:\n  - id: DS-0002\n"}
    ignored = await _run({"Dockerfile": _DOCKERFILE_ROOT}, config)
    assert "DS-0002" not in {f.rule_id for f in ignored}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_config_the_tool_rejects_is_rerun_once_with_the_default_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Valid YAML the classifier accepts, but Trivy rejects (unknown severity).
    config = {"trivy.yaml": b"severity:\n  - NOT-A-SEVERITY\n"}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.trivy"):
        findings = await _run({"Dockerfile": _DOCKERFILE_ROOT}, config)

    assert "DS-0002" in {f.rule_id for f in findings}
    reasons = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert "rejected_by_tool" in reasons


@pytest.mark.asyncio
@pytest.mark.integration
async def test_custom_rego_checks_are_skipped_not_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: a Rego check outside the scan directory that would flag every
    Dockerfile exists on disk; it must neither be loaded nor change the result."""
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "evil.rego").write_text(
        "# METADATA\n# title: canary\n# custom:\n#   id: CANARY-1\n#   severity: CRITICAL\n"
        "#   input:\n#     selector:\n#     - type: dockerfile\n"
        "package user.canary\n\nimport rego.v1\n\ndeny contains res if {\n"
        '  res := result.new("canary check fired", {})\n}\n'
    )
    config = {"trivy.yaml": f"rego:\n  check:\n    - {policies}\n".encode()}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.trivy"):
        findings = await _run({"Dockerfile": _DOCKERFILE_ROOT}, config)

    assert "CANARY-1" not in {f.rule_id for f in findings}
    assert "DS-0002" in {f.rule_id for f in findings}  # default profile ran
    assert "custom_checks_not_allowed" in [
        r.__dict__.get("config_skip_reason") for r in caplog.records
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_output_and_cache_paths_outside_the_scan_directory_create_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: a config naming an output file, a cache directory and a module
    directory outside the scan directory must not create anything there."""
    outside = tmp_path / "outside"
    config = {
        "trivy.yaml": (
            f"output: {outside}/report.json\ncache:\n  dir: {outside}/cache\n"
            f"module:\n  dir: {outside}/modules\n"
        ).encode()
    }

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.trivy"):
        findings = await _run({"Dockerfile": _DOCKERFILE_ROOT}, config)

    assert "DS-0002" in {f.rule_id for f in findings}
    assert "path_option_not_allowed" in [
        r.__dict__.get("config_skip_reason") for r in caplog.records
    ]
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cache_dir_in_an_honored_config_is_overridden_not_used(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    config = {"trivy.yaml": f"severity: [HIGH]\ncache:\n  dir: {outside}/cache\n".encode()}

    findings = await _run({"Dockerfile": _DOCKERFILE_ROOT}, config)

    assert findings
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dash_prefixed_filename_is_a_path_not_a_flag() -> None:
    findings = await _run({"-Dockerfile.prod": _DOCKERFILE_ROOT})
    assert {f.file for f in findings} <= {"-Dockerfile.prod"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_terraform_remote_modules_do_not_break_the_scan() -> None:
    """Offline: registry and git module sources cannot be fetched (dead proxy,
    no git); the scan still completes and reports the local resource. Run under
    ``docker run --network none`` in CI-equivalent verification."""
    content = {
        "main.tf": (
            'module "vpc" {\n  source  = "terraform-aws-modules/vpc/aws"\n  version = "5.0.0"\n}\n'
            'module "g" {\n  source = "git::https://example.com/x/y.git"\n}\n' + _BUCKET_ACL
        )
    }

    findings = await _run(content)

    assert any(f.rule_id == "AWS-0086" for f in findings)


def test_non_local_module_sources_are_neutralized_but_in_dir_ones_kept() -> None:
    src = (
        'module "a" {\n  source = "127.0.0.1:8099/ns/name/aws"\n}\n'
        'module "b" {\n  version = "1"\n  source = "git::https://x/y.git"\n}\n'
        'module "c" {\n  source = "./local"\n}\n'
        'module "d" {\n  source = "../up"\n}\n'
    )
    out = _neutralize_remote_modules({"m.tf": src.encode(), "x.txt": src.encode()})

    text = out["m.tf"].decode()
    assert text.count("./.review-remote-module-disabled") == 3
    assert '"./local"' in text and '"../up"' not in text
    assert "127.0.0.1" not in text and "x/y.git" not in text
    assert text.count("\n") == src.count("\n")
    assert out["x.txt"] == src.encode()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_loopback_module_source_never_connects() -> None:
    """Go never proxies loopback, so the dead proxy alone does not stop this."""
    connections: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(b"x")
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        content = {
            "main.tf": f'module "a" {{\n  source = "127.0.0.1:{port}/ns/n/aws"\n}}\n' + _BUCKET_ACL
        }
        findings = await _run(content)
    finally:
        server.close()
        await server.wait_closed()

    assert any(f.rule_id == "AWS-0086" for f in findings)
    assert connections == []


# --- integration: through the real step (US-A1, US-A3) ----------------------------


@dataclass
class _PrefilterOutput:
    diff: Diff
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    base_sha: str | None = None
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None
    private: bool = False


class _FetchesContent:
    """A ``DiffFetcher`` fake: ``StaticAnalysisStep`` only calls ``fetch_content``."""

    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        raise NotImplementedError("StaticAnalysisStep never calls DiffFetcher.fetch()")

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        return dict(self._content)


class _PassthroughRedactor:
    async def redact(self, text: str) -> str:
        return text


class _CountingTrivy(TrivyAnalyzer):
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Any,
    ) -> list[Finding]:
        self.calls += 1
        return await super().run(diff, content, timeout=timeout, config_files=config_files)


def _step(content: dict[str, str], trivy: TrivyAnalyzer) -> StaticAnalysisStep:
    return StaticAnalysisStep(
        _FetchesContent({path: text.encode() for path, text in content.items()}),
        registrations=[
            AnalyzerRegistration(
                "trivy",
                trivy,
                matches=match_iac,
                security_class=True,
                config_filenames=TRIVY_CONFIG_FILENAMES,
            )
        ],
        redactor=_PassthroughRedactor(),
        enabled={"trivy": True},
        timeouts={"trivy": 60.0},
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_iac_only_pr_posts_only_trivy_findings_on_added_lines() -> None:
    content = {"main.tf": _TERRAFORM, "app.py": "print(1)\n", "notes.yaml": "name: x\n"}
    diff = Diff(
        files=(
            _added("main.tf", _TERRAFORM),
            _added("app.py", "print(1)\n"),
            _added("notes.yaml", "name: x\n"),
        )
    )
    trivy = _CountingTrivy()

    output = await _step(content, trivy).run(_PrefilterOutput(diff=diff), StepContext(job_id="j"))

    assert trivy.calls == 1
    assert output.findings
    assert {f.file for f in output.findings} == {"main.tf"}
    assert all(f.source == "trivy" and f.line is not None for f in output.findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_helm_chart_and_cloudformation_never_invoke_trivy() -> None:
    helm_template = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - securityContext:\n"
        "            privileged: true\n"
    )
    cloudformation = (
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  B:\n    Type: AWS::S3::Bucket\n"
        "    Properties:\n      AccessControl: PublicRead\n"
    )
    content = {
        "chart/Chart.yaml": "apiVersion: v2\nname: demo\nversion: 0.1.0\n",
        "chart/values.yaml": "replicas: 1\n",
        "chart/templates/deployment.yaml": helm_template,
        "cfn/template.yaml": cloudformation,
        "cfn/template.json": '{"Resources": {}}',
    }
    diff = _diff_of(content)
    trivy = _CountingTrivy()

    output = await _step(content, trivy).run(_PrefilterOutput(diff=diff), StepContext(job_id="j"))

    assert trivy.calls == 0
    assert output.findings == []


def test_result_outside_the_scan_directory_is_dropped_not_fatal() -> None:
    patch = "@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n"
    findings, _ = _to_findings(
        [
            _result("../other/m.tf", _entry()),
            _result("/other/m.tf", _entry()),
            _result("main.tf", _entry()),
        ],
        {},
        {"main.tf": patch},
    )
    assert [f.file for f in findings] == ["main.tf"]


def test_pathologically_nested_trivy_config_falls_back_to_default() -> None:
    deep = b"[" * 100_000 + b"]" * 100_000
    for name in ("trivy.yaml", ".trivyignore.yaml"):
        choice = classify_trivy_config({name: deep})
        assert (choice.config_name, choice.ignore_name) == (None, None)
        assert choice.outcome.source == "default"
        assert choice.outcome.skip_reason == "config_invalid"


@pytest.mark.parametrize(
    "text",
    [
        "Output: /tmp/x\n",
        "REGO:\n  Check: [/etc]\n",
        "Cacert: /etc/ssl/ca.pem\n",
        "Ignore-Policy: /etc/p.rego\n",
        "Misconfiguration:\n  Checks-Bundle-Repository: evil.example/x\n",
    ],
)
def test_config_option_keys_are_matched_case_insensitively(text: str) -> None:
    choice = _classify({"trivy.yaml": text})

    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is not None


def test_alias_bomb_config_is_classified_quickly() -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 12)]

    choice = _classify({"trivy.yaml": "\n".join(lines) + "\n"})

    assert choice.outcome.source == "repo"


def test_module_source_is_neutralized_after_a_nested_brace() -> None:
    src = (
        'module "x" {\n  for_each = { a = 1 }\n  providers = { aws = aws.x }\n'
        '  # a "quote in a comment\n  source = "http://127.0.0.1:8080/m"\n}\n'
        'module "y" {\n  source = "./ok"\n}\n'
    )
    js = '{"module": {"j": {"source": "http://127.0.0.1:1/m"}, "k": [{"source": "./ok"}]}}'

    out = _neutralize_remote_modules({"m.tf": src.encode(), "m.tf.json": js.encode()})

    text = out["m.tf"].decode()
    assert "127.0.0.1" not in text and '"./ok"' in text
    assert text.count("\n") == src.count("\n")
    assert b"127.0.0.1" not in out["m.tf.json"] and b'"./ok"' in out["m.tf.json"]
