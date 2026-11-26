"""
Every product-knowledge bootstrap variable is documented in ``.env.example``.

Explicit assertion for the Worker and the Admin API (US-PK13).

``apps/test_env_example_sync.py`` already covers the Worker generically but
deliberately excludes ``AdminAPISettings``; this test pins the new
``REVIEW_WORKER_PRODUCT_KNOWLEDGE_*`` / ``REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_*``
variables independently of any unrelated drift in that file.
"""

from __future__ import annotations

import re
from pathlib import Path

from apps.admin_api.settings import AdminAPISettings
from apps.worker.settings import WorkerSettings

_ROOT = Path(__file__).resolve().parents[1]
_ENV_KEY_RE = re.compile(r"^([A-Z0-9_]+)=", re.MULTILINE)


def _documented(path: Path) -> set[str]:
    return set(_ENV_KEY_RE.findall(path.read_text()))


def _pk_vars(settings_cls: type, prefix: str) -> set[str]:
    return {
        f"{prefix}{name.upper()}"
        for name in settings_cls.model_fields  # type: ignore[attr-defined]
        if name.startswith("product_knowledge_")
    }


def test_worker_product_knowledge_vars_are_in_env_example() -> None:
    expected = _pk_vars(WorkerSettings, "REVIEW_WORKER_")

    assert "REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS" in expected
    assert "REVIEW_WORKER_PRODUCT_KNOWLEDGE_DEADLINE_SECONDS" in expected
    assert expected <= _documented(_ROOT / ".env.example")


def test_admin_api_product_knowledge_vars_are_in_env_example() -> None:
    expected = _pk_vars(AdminAPISettings, "REVIEW_ADMIN_API_")

    assert "REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS" in expected
    # The deadline is Worker-only.
    assert "REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_DEADLINE_SECONDS" not in expected
    assert expected <= _documented(_ROOT / ".env.example")


def test_compose_passes_every_product_knowledge_var_to_its_service() -> None:
    compose = (_ROOT / "docker-compose.yml").read_text()
    expected = _pk_vars(WorkerSettings, "REVIEW_WORKER_") | _pk_vars(
        AdminAPISettings, "REVIEW_ADMIN_API_"
    )

    missing = {var for var in expected if f"{var}:" not in compose}
    assert not missing, f"docker-compose.yml does not pass: {missing}"


def test_configmap_template_has_no_folded_scalar_around_quote() -> None:
    # A YAML `>-` block does not interpret quotes, so `>-` + `{{ x | quote }}`
    # renders the literal `"5"` and fails settings validation at startup.
    # helm is not required: this is a template-text invariant.
    text = (_ROOT / "charts/review/templates/configmap.yaml").read_text()

    assert not re.search(r":\s*>-?\s*\n\s*\{\{[^\n]*\|\s*quote\s*\}\}", text)
