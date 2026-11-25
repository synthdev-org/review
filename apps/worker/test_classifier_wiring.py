"""Unit tests for the Worker's classifier wiring (plan Phase 10): no singletons, bounded slots."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

import apps.worker.main as worker_main
from adapter.storage.tenancy import PostgresSettingRepository
from apps.worker.settings import WorkerSettings
from findings.redact import NoOpTextRedactor
from review.finding_judge import ClassifierVerification
from tenancy.settings import SettingRow


class _EmptyRepository:
    async def list_rows_for_key(
        self, *, key: str, org_id: str | None, repo_id: str | None
    ) -> tuple[SettingRow, ...]:
        return ()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")


def _build(settings: WorkerSettings) -> ClassifierVerification:
    return worker_main._build_classifier_verification(
        settings, cast(PostgresSettingRepository, _EmptyRepository()), NoOpTextRedactor()
    )


@pytest.mark.asyncio
async def test_setting_absent_yields_no_classifier() -> None:
    wiring = _build(WorkerSettings(_env_file=None))

    assert await wiring.resolver.resolve("11111111-1111-1111-1111-111111111111") is None


def test_each_build_owns_its_semaphores_sized_from_the_pinned_defaults() -> None:
    settings = WorkerSettings(_env_file=None)

    first, second = _build(settings), _build(settings)

    assert first.classifier_slots is not second.classifier_slots
    assert first.redaction_slots is not second.redaction_slots
    assert first.classifier_slots._value == 8
    assert first.redaction_slots._value == 4
    assert (first.max_state_bytes, first.timeout) == (8000, 10.0)


def test_a_zero_slot_count_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_WORKER_CLASSIFIER_MAX_CONCURRENCY", "0")

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)
