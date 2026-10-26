"""Local-distribution E2E (docs/rules/testing.md, ADR-0016): the built local-mode distribution,
installed into a clean container with no services, reviews a real git repository against a
recorded model.

The image installs only a built wheel (so package data and the console script are what a user
gets), then the source is deleted, so nothing runs in place. The only things the container can
reach are the stub model on the host and the files mounted into it. Gated like the Compose E2E:
on merge to main or nightly (.github/workflows/e2e.yml runs this directory).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from adapter.local.conftest import feature_branch_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "review-local-e2e:latest"
_BUILD_TIMEOUT_SECONDS = 900.0
_RUN_TIMEOUT_SECONDS = 180.0

# gitleaks is a Go binary the CLI shells out to; it is vendored the way the server image does it.
_DOCKERFILE = """\
FROM zricethezav/gitleaks:v8.21.2 AS gitleaks
FROM python:3.12-slim AS wheel
COPY . /src
RUN pip wheel --no-cache-dir --no-deps /src -w /dist

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=wheel /dist /dist
RUN pip install --no-cache-dir /dist/*.whl && rm -rf /dist
WORKDIR /work
"""

# The documented recommended route (docs/guides/local-mode-quickstart.md): pipx on a chosen Python.
# A separate file because the legacy (non-BuildKit) builder builds every stage, and the 3.13 build
# is expected to fail.
_PIPX_DOCKERFILE = """\
ARG PYTHON=3.12
FROM python:3.12-slim AS wheel
COPY . /src
RUN pip wheel --no-cache-dir --no-deps /src -w /dist

FROM python:${PYTHON}-slim
COPY --from=wheel /dist /dist
ENV PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
RUN pip install --no-cache-dir pipx && pipx install /dist/*.whl
"""

_MODEL_REQUESTS: list[str] = []  # system prompts the recorded model has served

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")


def _completion(content: str) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": 0,
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


class _RecordedModel(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        system = body["messages"][0]["content"]
        _MODEL_REQUESTS.append(system)
        if "verification assistant" in system:
            verdict = {"index": 0, "valid": True, "actionable": True, "specific": True}
            content = json.dumps({"verdicts": [{**verdict, "valuable": True}]})
        elif "summary (2-4 sentences)" in system:
            content = "Rewrites total() as an explicit loop."
        else:
            finding = {"file": "app.py", "line": 2, "severity": "high", "message": "e2e finding"}
            content = json.dumps({"findings": [finding]})
        payload = _completion(content)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def model_port() -> Iterator[int]:
    # Every interface, so traffic arriving over Docker's host-gateway bridge reaches it.
    server = HTTPServer(("0.0.0.0", 0), _RecordedModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> str:
    dockerfile = tmp_path_factory.mktemp("local-e2e") / "Dockerfile"
    dockerfile.write_text(_DOCKERFILE)
    subprocess.run(
        ["docker", "build", "-q", "-t", IMAGE, "-f", str(dockerfile), str(REPO_ROOT)],
        check=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    return IMAGE


def _run(
    image: str,
    model_port: int,
    repo: Path,
    *args: str,
    writable: bool = False,
    model: bool = True,
) -> subprocess.CompletedProcess[str]:
    """`review <args>` in the clean container against ``repo`` mounted at /work."""
    env = ["-e", "HOME=/tmp", "-e", "REVIEW_LOCAL_STATIC_ANALYSIS_ENABLED=false"]
    if model:
        endpoint = f"http://host.docker.internal:{model_port}/v1"
        env += ["-e", f"REVIEW_LOCAL_LLM_ENDPOINT={endpoint}"]
        env += ["-e", "REVIEW_LOCAL_LLM_MODEL=local-model"]
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "--add-host", "host.docker.internal:host-gateway",
            # The repository is owned by the host user; run as them so git does not refuse it.
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{repo}:/work{'' if writable else ':ro'}",
            *env,
            image,
            "review", *args,
        ],
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    if shutil.which("git") is None:
        pytest.skip("git is not installed on the host that prepares the fixture repository")
    # The sibling `docker run` is resolved by the host daemon, so the repository must live at a
    # path the host can see. On a host that is the ordinary tmp_path. When pytest itself runs in a
    # container, set REVIEW_E2E_SHARED_TMP to a directory bind-mounted at the same path on both
    # sides (/tmp/review-run.sh does this); a container-private /tmp would mount as an empty dir.
    shared = os.environ.get("REVIEW_E2E_SHARED_TMP")
    if not shared:
        yield feature_branch_repo(tmp_path / "repo").path
        return
    workdir = Path(tempfile.mkdtemp(dir=shared))
    try:
        yield feature_branch_repo(workdir / "repo").path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_the_built_distribution_reviews_a_branch_in_a_clean_container(
    image: str, model_port: int, repo: Path
) -> None:
    result = _run(image, model_port, repo, "run", "--base", "main")

    assert result.returncode == 0, result.stderr
    assert "app.py:2: high: e2e finding" in result.stdout
    # Findings and their count go to stdout; progress and the run summary go to stderr.
    assert "1 finding(s)." in result.stdout
    assert "truncation: none" in result.stderr and "memory file:" in result.stderr
    assert "e2e finding" not in result.stderr


def test_the_installed_distribution_reviews_staged_changes(
    image: str, model_port: int, repo: Path
) -> None:
    (repo / "app.py").write_text("def total(items):\n    result = 1\n    return result\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, timeout=30)

    result = _run(image, model_port, repo, "run", "--staged")

    assert result.returncode == 0, result.stderr
    assert "app.py:2: high: e2e finding" in result.stdout


def test_the_installed_distribution_writes_markdown_and_json_output_files(
    image: str, model_port: int, repo: Path
) -> None:
    # The container runs as the host user; a world-writable mount lets it create the files.
    repo.chmod(0o777)
    for name in ("review.md", "review.json"):
        result = _run(image, model_port, repo, "run", "--base", "main", "--output", name,
                      writable=True)  # fmt: skip
        assert result.returncode == 0, result.stderr

    assert "e2e finding" in (repo / "review.md").read_text()
    assert "e2e finding" in json.dumps(json.loads((repo / "review.json").read_text()))


def test_the_installed_distribution_dry_run_sends_nothing_to_the_model(
    image: str, model_port: int, repo: Path
) -> None:
    before = len(_MODEL_REQUESTS)

    result = _run(image, model_port, repo, "run", "--base", "main", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert len(_MODEL_REQUESTS) == before


def test_the_installed_distribution_refuses_to_run_without_a_model(
    image: str, model_port: int, repo: Path
) -> None:
    result = _run(image, model_port, repo, "run", "--base", "main", model=False)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert "REVIEW_LOCAL_LLM_ENDPOINT" in result.stderr


def test_the_installed_distribution_reports_an_unknown_base_ref_as_a_usage_error(
    image: str, model_port: int, repo: Path
) -> None:
    before = len(_MODEL_REQUESTS)

    result = _run(image, model_port, repo, "run", "--base", "no-such-ref")

    assert result.returncode == 2
    assert "no-such-ref" in result.stderr
    assert len(_MODEL_REQUESTS) == before


def test_the_installed_console_script_has_a_version_and_documents_its_environment(
    image: str,
) -> None:
    def run(*args: str) -> str:
        done = subprocess.run(
            ["docker", "run", "--rm", image, "review", *args],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=True,
        )
        return done.stdout

    assert run("--version").startswith("review ")
    help_text = run("run", "--help")
    assert "REVIEW_LOCAL_LLM_ENDPOINT" in help_text and "exit codes:" in help_text


_PACKAGING_PROBE = """\
from importlib.metadata import entry_points
from pathlib import Path
import adapter.analyzers as a
eps = {e.name: e.value for e in entry_points(group="console_scripts")}
assert eps["review"] == "apps.local_cli.main:console_main", eps
d = Path(a.__file__).parent
need = ["eslint_default.json", "golangci_lint_default.yml", "phpstan_default.neon",
        "ruff_default.toml", "semgrep_ruleset.yaml", "trivy_default.yaml"]
missing = [n for n in need if not (d / n).is_file()]
assert not missing, missing
print("ok")
"""


def test_the_wheel_ships_the_console_entry_point_and_the_analyzer_profiles(image: str) -> None:
    done = subprocess.run(
        ["docker", "run", "--rm", image, "python", "-I", "-c", _PACKAGING_PROBE],
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )

    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def _pipx_install(python: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "build", "--build-arg", f"PYTHON={python}",
         "-t", f"review-local-e2e-pipx{python}", "-f", "-", str(REPO_ROOT)],
        input=_PIPX_DOCKERFILE,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )  # fmt: skip


def test_pipx_installs_the_wheel_on_python_3_12_and_the_command_runs() -> None:
    built = _pipx_install("3.12")

    assert built.returncode == 0, built.stderr[-2000:]
    done = subprocess.run(
        ["docker", "run", "--rm", "review-local-e2e-pipx3.12", "review", "--version"],
        capture_output=True, text=True, timeout=_RUN_TIMEOUT_SECONDS, check=False,
    )  # fmt: skip
    assert done.stdout.startswith("review "), done.stderr


def test_pipx_on_python_3_13_fails_with_the_documented_message() -> None:
    built = _pipx_install("3.13")

    assert built.returncode != 0
    assert "needs a Python matching" in built.stdout + built.stderr
