"""Top-level test suite package.

Per docs/rules/testing.md: unit/integration tests are co-located inside
each package under a first-party top-level directory (`adapter/`,
`apps/`, ...); this `tests/` package holds only the E2E tier
(`tests/e2e/`), which drives the built container images rather than
in-process code, so it does not belong inside any single package.
"""
