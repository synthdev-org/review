"""
Runs ``charts/review/test-render.sh`` from pytest when helm is installed.

The script holds the helm lint/template assertions, including the product-knowledge ones; CI also
runs it directly in the ``helm-lint`` job.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "charts" / "review" / "test-render.sh"


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("docker") is None,
    reason="helm and docker (compose) are required by test-render.sh",
)
def test_chart_render_assertions_pass() -> None:
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True, timeout=300, check=False)

    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "FAIL:" not in result.stdout


# --- plan-phase-9 Phase 4: the worker-only `worker-runtime` image target -----------
# Static checks of the files themselves, so they run without helm or docker.

ROOT = SCRIPT.parents[2]


def test_only_the_worker_service_builds_the_worker_runtime_target() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    assert services["worker"]["build"]["target"] == "worker-runtime"
    assert services["worker"]["image"] == "review-worker:compose-local"
    for name in ("migrate", "webhook_api", "admin_api"):
        assert services[name]["build"]["target"] == "runtime"
        assert services[name]["image"] == "review-app:compose-local"


def test_helm_worker_deployment_uses_the_worker_image_and_others_do_not() -> None:
    values = yaml.safe_load((ROOT / "charts" / "review" / "values.yaml").read_text())

    assert values["worker"]["image"]["repository"].endswith("/review-worker")
    assert values["webhookApi"]["image"]["repository"].endswith("/review")
    assert values["adminApi"]["image"]["repository"].endswith("/review")


def test_worker_runtime_stage_extends_runtime_and_is_the_only_one_with_node() -> None:
    stages = re.findall(
        r"^FROM\s+(\S+)\s+AS\s+(\S+)\s*$", (ROOT / "Dockerfile").read_text(), re.MULTILINE
    )

    assert ("runtime", "worker-runtime") in stages
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime_stage = dockerfile.split("FROM base AS runtime")[1].split("FROM runtime AS")[0]
    assert "node-runtime" not in runtime_stage
    assert "eslint-tool" not in runtime_stage
    assert "php-runtime" not in runtime_stage
    assert "phpstan-tool" not in runtime_stage
    assert "--from=node-runtime" in dockerfile.split("FROM runtime AS worker-runtime")[1]
    worker_stage = dockerfile.split("FROM runtime AS worker-runtime")[1]
    assert "--from=php-runtime" in worker_stage
    assert "--from=phpstan-tool" in worker_stage
    assert "go-toolchain" not in runtime_stage
    assert "golangci-tool" not in runtime_stage
    assert "--from=go-toolchain" in worker_stage
    assert "--from=golangci-tool" in worker_stage
