"""
Unit tests for ``review.step.JobIdentity`` (product-knowledge capture fields).

Every subclass carries the capture fields, and no
model in the REVIEW chain forbids extra keys (which is what makes a Webhook-first rolling deploy
safe).
"""

from __future__ import annotations

import importlib
import inspect
import typing

import pytest
from pydantic import BaseModel

from review.step import JobIdentity, job_identity_fields

_STEP_MODULES = (
    "review.prefilter",
    "review.static_analysis",
    "review.context_builder",
    "review.intent_summary",
    "review.review_generation",
    "review.verification",
)
_CAPTURE: dict[str, typing.Any] = {
    "pr_title": "Fix PROJ-9",
    "pr_head_ref": "bugfix/PROJ-9",
    "pr_issue_keys": ("PROJ-9", "#41"),
    "pr_is_fork": False,
    "private": True,
}
_IDENTITY: dict[str, typing.Any] = {
    "org_id": "o",
    "repo_id": "r",
    "installation_id": 1,
    "repo_full_name": "acme/widgets",
    "pr_number": 1,
    "head_sha": "h",
    "base_sha": "b",
}


def _module_models() -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    for name in _STEP_MODULES:
        module = importlib.import_module(name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == name:
                models.append(obj)
    return models


def _job_identity_subclasses() -> list[type[JobIdentity]]:
    _module_models()  # import side effect: ensure every subclass is loaded
    found: list[type[JobIdentity]] = []
    stack = list(JobIdentity.__subclasses__())
    while stack:
        cls = stack.pop()
        found.append(cls)
        stack.extend(cls.__subclasses__())
    return sorted(set(found), key=lambda c: c.__qualname__)


def _required_extras(cls: type[JobIdentity]) -> dict[str, typing.Any]:
    """Placeholder values for any required non-identity field."""
    extras: dict[str, typing.Any] = {}
    for name, field in cls.model_fields.items():
        if name in JobIdentity.model_fields or not field.is_required():
            continue
        extras[name] = None
    return extras


def test_job_identity_subclasses_are_discovered() -> None:
    names = {c.__name__ for c in _job_identity_subclasses()}
    assert {"PrefilterOutput", "StaticAnalysisOutput", "ContextBuilderOutput"} <= names


@pytest.mark.parametrize("cls", _job_identity_subclasses(), ids=lambda c: c.__qualname__)
def test_every_subclass_round_trips_capture_fields(cls: type[JobIdentity]) -> None:
    # model_construct skips validation of step-specific required fields; the
    # point is that the identity fields exist on, and survive through, every
    # subclass and `job_identity_fields`.
    built = cls.model_construct(**_IDENTITY, **_CAPTURE, **_required_extras(cls))

    forwarded = job_identity_fields(built)

    for key, value in _CAPTURE.items():
        assert getattr(built, key) == value
        assert forwarded[key] == value


def test_capture_fields_default_when_absent() -> None:
    identity = JobIdentity(**_IDENTITY)
    assert identity.pr_title is None
    assert identity.pr_head_ref is None
    assert identity.pr_issue_keys == ()
    assert identity.pr_is_fork is None
    assert identity.private is False


def test_no_review_chain_model_forbids_extra_keys() -> None:
    models = _module_models() + _job_identity_subclasses()
    assert models
    offenders = [m.__qualname__ for m in models if m.model_config.get("extra") == "forbid"]
    assert offenders == []
