"""
pytest options for ``test_conformance.py`` (plan Phase 5, US-PK17).

``--pk-url`` (or ``PK_URL``) opts the live-endpoint test in; the bearer token
is read from ``PK_TOKEN`` only, never from argv, so it cannot land in a
process listing or a CI log.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("product-knowledge")
    group.addoption(
        "--pk-url",
        default=os.environ.get("PK_URL"),
        help="MCP endpoint to run the live conformance check against (opt-in; never a CI gate).",
    )
    group.addoption(
        "--pk-query",
        default=os.environ.get("PK_QUERY", "product knowledge"),
        help="A search term the live endpoint answers with at least one result.",
    )
