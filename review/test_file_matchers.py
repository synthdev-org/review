"""Unit tests for review.file_matchers (US-A1 detection rules)."""

from __future__ import annotations

import pytest

from review.file_matchers import (
    match_all,
    match_go,
    match_iac,
    match_javascript,
    match_php,
    match_python,
)


@pytest.mark.parametrize(
    ("matcher", "path", "expected"),
    [
        (match_python, "a/b.py", True),
        (match_python, "a/b.pyc", False),
        (match_python, "b.PY", True),
        (match_javascript, "x.tsx", True),
        (match_javascript, "x.mjs", True),
        (match_javascript, "x.json", False),
        (match_go, "main.go", True),
        (match_go, "main.gox", False),
        (match_php, "i.php", True),
        (match_php, "i.phtml", False),
        (match_all, "anything.unknown", True),
    ],
)
def test_extension_matchers(matcher, path: str, expected: bool) -> None:  # type: ignore[no-untyped-def]
    assert matcher(path, b"") is expected


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("infra/main.tf", b"", True),
        ("infra/main.tf.json", b"", True),
        ("vars.tfvars", b"", True),
        ("Dockerfile", b"", True),
        ("svc/Dockerfile.prod", b"", True),
        ("build.dockerfile", b"", True),
        ("Dockerfilex", b"", False),
        ("deploy.yaml", b"apiVersion: v1\nkind: Pod\n", True),
        ("deploy.yml", b"---\nkind: Pod\napiVersion: v1\n", True),
        ("deploy.yaml", b"kind: Pod\n", False),
        ("chart/templates/d.yaml", b"apiVersion: v1\nkind: Pod\nname: {{ .Values.n }}\n", False),
        ("cfn.yaml", b"AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n", False),
        ("deploy.yaml", b"spec:\n  apiVersion: v1\n  kind: Pod\n", False),
        ("config.yaml", b"name: x\n", False),
        ("notes.txt", b"apiVersion: v1\nkind: Pod\n", False),
        ("-rf.tf", b"", True),  # leading dash is just a name here
        ("INFRA/MAIN.TF", b"", True),
        ("a/Dockerfile", b"", True),
        ("deploy.yaml", b"apiVersion : v1\nkind : Pod\n", True),
        ("deploy.yaml", b"", False),
    ],
)
def test_iac_matcher(path: str, content: bytes, expected: bool) -> None:
    assert match_iac(path, content) is expected
