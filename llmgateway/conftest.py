"""Test-collection setup for the `llmgateway` package.

`llmgateway/test_routing.py` needs `reset_otel_global_providers` to exercise
`llmgateway.routing`'s own module-level OTel counter against a fresh,
test-owned `MeterProvider` — imported here (not redefined) so this package
reuses the same fixture `review/conftest.py` already does, per
`docs/rules/testing.md`'s "no near-duplicate copy" convention.
"""

from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401
