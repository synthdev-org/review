"""adapter.telemetry — OpenTelemetry SDK/exporter construction, confined to
this adapter and the two composition roots that call it
(apps/webhook_api/main.py, apps/worker/main.py), per
docs/specs/spec-observability.md's Unit A and docs/rules/architecture.md's
composition-root wiring rule.

Unlike every other ``adapter/*`` package, this one is not a
``typing.Protocol`` implementation swapped in behind a domain-defined
interface — see that spec's "Resolved decisions" for why: OpenTelemetry's
own *API* package (``opentelemetry.trace``/``opentelemetry.metrics``) is
already a stable, vendor-neutral, no-op-by-default interface, imported
directly by domain code the same way ``logging`` already is. Only the SDK +
exporter *construction* (real infra: background export threads, network
clients) is infra-only and lives here.
"""
