"""review.file_matchers — pure file classification for static-analysis
language routing (spec US-A1). A matcher takes ``(path, content)`` and says
whether an analyzer should receive that file; detection is by name/extension
(plus a top-level ``apiVersion``/``kind`` sniff for Kubernetes YAML), and an
unknown extension never matches an engine.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable

FileMatcher = Callable[[str, bytes], bool]

_K8S_KEYS = (re.compile(rb"^apiVersion\s*:", re.MULTILINE), re.compile(rb"^kind\s*:", re.MULTILINE))


def match_all(path: str, content: bytes) -> bool:
    return True


def _by_extension(*extensions: str) -> FileMatcher:
    suffixes = tuple(extensions)

    def matcher(path: str, content: bytes) -> bool:
        return path.lower().endswith(suffixes)

    return matcher


match_python = _by_extension(".py")
match_javascript = _by_extension(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
match_go = _by_extension(".go")
match_php = _by_extension(".php")


def match_iac(path: str, content: bytes) -> bool:
    """Terraform, Dockerfiles, and Kubernetes manifests (YAML carrying both a
    top-level ``apiVersion`` and ``kind``)."""
    lowered = path.lower()
    if lowered.endswith((".tf", ".tf.json", ".tfvars", ".dockerfile")):
        return True
    base = posixpath.basename(path)
    if base == "Dockerfile" or base.startswith("Dockerfile."):
        return True
    if lowered.endswith((".yaml", ".yml")):
        # `{{` marks a Helm (Go-template) manifest: not plain Kubernetes YAML, out of scope.
        return b"{{" not in content and all(key.search(content) for key in _K8S_KEYS)
    return False
