"""Regression test: the stand-in model shipped with the quickstart (docs/guides/stub_model.py)
is enough, as written, for a whole `review run` to succeed. It was missing from the guide, and a
stub that answered every call alike failed at the verification step.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest

from adapter.local.conftest import feature_branch_repo, requires_git
from apps.local_cli.main import EXIT_OK, run_review
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "guides" / "stub_model.py"


@pytest.fixture
def guide_stub_endpoint() -> Iterator[str]:
    spec = importlib.util.spec_from_file_location("guide_stub_model", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = HTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


@pytest.mark.asyncio
async def test_the_quickstart_stub_model_completes_a_review_with_its_canned_finding(
    tmp_path: Path, guide_stub_endpoint: str
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = LocalSettings(
        llm_endpoint=guide_stub_endpoint,
        llm_model="m",
        static_analysis_enabled=False,
        llm_timeout_seconds=30.0,
    )
    out, err = io.StringIO(), io.StringIO()

    status = await run_review(settings, base="main", repo_dir=repo.path, out=out, err=err)

    assert status == EXIT_OK, err.getvalue()
    assert out.getvalue().splitlines() == ["app.py:2: high: canned finding", "1 finding(s)."]
