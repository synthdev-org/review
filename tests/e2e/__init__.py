"""E2E tests: drive the Docker Compose stack over HTTP, per
docs/rules/testing.md's top tier. Not collected by a bare `pytest`/`uv run
pytest` invocation (pyproject.toml's `testpaths` lists only the
first-party package directories) — run explicitly via
`uv run pytest tests/e2e`, as .github/workflows/e2e.yml does on merge to
main.
"""
