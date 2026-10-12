"""
Runs ``charts/review/test-render.sh`` from pytest when helm is installed.

The script holds the helm lint/template assertions, including the product-knowledge ones; CI also
runs it directly in the ``helm-lint`` job.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "charts" / "review" / "test-render.sh"


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("docker") is None,
    reason="helm and docker (compose) are required by test-render.sh",
)
def test_chart_render_assertions_pass() -> None:
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True, timeout=300, check=False)

    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "FAIL:" not in result.stdout
