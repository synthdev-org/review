"""Integration tests for US-L6 (use the model I already have): any OpenAI-compatible endpoint
through the existing ``LiteLLMClient``, only that host contacted, and an endpoint that does not
answer fails visibly within ``llm_timeout_seconds``.

The same real-git, real-registry, local-stub setup as ``test_local_review.py``; the stub's request
capture (adapter/llm/conftest.py) shows what the endpoint received.
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import time
from pathlib import Path

import pytest

from adapter.llm.conftest import CapturedRequest, StartCapturingStub
from adapter.local.conftest import Repo, feature_branch_repo, requires_git
from apps.local_cli.main import EXIT_OK, EXIT_RUNTIME_FAILURE, run_review
from apps.local_cli.settings import LocalSettings
from apps.local_cli.test_local_review import _FINDING, _model_reply

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


@pytest.fixture
def contacted(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every host looked up and every address connected to while a test runs."""
    seen: list[object] = []
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        seen.append(address)
        real_connect(self, address)  # type: ignore[arg-type]

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        seen.append(host)
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


async def _run(repo: Repo, settings: LocalSettings) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(settings, base="main", repo_dir=repo.path, out=out, err=err)
    return status, out.getvalue(), err.getvalue()


def _settings(endpoint: str, *, timeout: float = 30.0) -> LocalSettings:
    return LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=False,
        llm_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_us_l6_review_completes_on_the_configured_endpoint_and_contacts_no_other_host(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    contacted: list[object],
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model_reply)

    status, out, _err = await _run(repo, _settings(endpoint))

    assert status == EXIT_OK
    assert out.splitlines() == [_FINDING, "1 finding(s)."]
    assert llm_requests and {r.path for r in llm_requests} == {"/v1/chat/completions"}
    assert {r.json()["model"] for r in llm_requests} == {"local-model"}
    stub_port = int(endpoint.split(":")[2].split("/")[0])
    addresses = [a for a in contacted if isinstance(a, tuple)]
    hosts = [a for a in contacted if isinstance(a, str)]
    assert addresses, "the capture saw no connection, so it proves nothing"
    assert {(a[0], a[1]) for a in addresses} == {("127.0.0.1", stub_port)}
    assert set(hosts) <= {"127.0.0.1"}


@pytest.mark.asyncio
async def test_us_l6_an_endpoint_that_never_answers_fails_within_the_timeout_naming_it(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    def hang(_request: CapturedRequest) -> tuple[int, bytes]:
        time.sleep(8)
        return 500, b"{}"

    endpoint = stub_llm_capture(hang)
    timeout = 1.0

    started = time.monotonic()
    status, out, err = await _run(repo, _settings(endpoint, timeout=timeout))
    elapsed = time.monotonic() - started

    assert status == EXIT_RUNTIME_FAILURE
    assert elapsed < timeout + 3  # one timed-out call plus git/gitleaks work, not the 4s hang
    assert endpoint in err
    assert "Traceback" not in err
    assert out == ""  # nothing that could be read as a completed review
