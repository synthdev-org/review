"""Test-collection setup for the webhook_api test package.

`apps/webhook_api/main.py` parses `WebhookAPISettings()` at import time (it
needs a module-level `app` object for uvicorn to discover as
`apps.webhook_api.main:app`), so any test module that imports it needs
`REVIEW_WEBHOOK_API_ENVIRONMENT` and (as of Phase 4)
`REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET` set before that import happens —
the latter is a required field with no default (see settings.py). pytest
imports `conftest.py` before collecting sibling test modules in this
directory, so setting defaults here (only if not already set) covers every
test file in this package without each one repeating it. Tests that need a
*specific* secret/settings value (e.g. to compute a matching webhook
signature) construct their own `WebhookAPISettings(...)` directly instead
of relying on these process-wide defaults.
"""

import os

os.environ.setdefault("REVIEW_WEBHOOK_API_ENVIRONMENT", "development")
os.environ.setdefault("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "conftest-default-webhook-secret")
