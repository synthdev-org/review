# Security Policy

review runs inside your infrastructure and handles your source code. The maintainers take security reports seriously.

## Supported versions

The maintainers fix security problems in the latest code on the `main` branch and in the latest release. review is before version 1.0, so older releases do not get fixes. To get a fix, upgrade to the latest release.

## Report a vulnerability

Do not open a public issue, a public pull request, or a public discussion for a vulnerability. A public report gives attackers the details before a fix exists.

1. Open the Security tab of the review repository on GitHub.
2. Select "Report a vulnerability".
3. Describe the problem.

Include these items when you can:

- The affected component, file, or route.
- The version or the commit that you tested.
- The steps to reproduce the problem, or a proof of concept.
- The impact. For example, state what data an attacker can read or change.
- Your name or handle, if you want credit.

Do not include real secrets or real customer data in your report.

## What happens next

1. The maintainers will reply within 5 business days to confirm that they received your report.
2. The maintainers will investigate and tell you whether they accept the report as a vulnerability.
3. If they accept it, they will work on a fix and tell you the plan. You can ask for updates at any time.
4. When the fix is ready, the maintainers will publish a release and a GitHub security advisory. They will credit you if you want credit.

Please give the maintainers a reasonable time to fix the problem before you share the details in public. The maintainers will agree on a date with you.

## Scope

These parts of review are in scope:

- The Webhook API, including webhook signature checks and rate limits.
- The Worker and the review pipeline.
- The Admin API and the Admin UI, including local sign-in, OIDC sign-in, sessions, roles, and CSRF protection.
- The Helm chart and the Docker Compose file, when they create an unsafe default.
- The handling of secrets, including secret redaction and the checks on outgoing network calls to optional providers.

These items are not in scope:

- Problems in a third-party service or library that you can reproduce without review. Report those to the owner of that project.
- Problems that need an attacker to already have admin access to the Admin UI or access to your database.
- Findings from an automated scanner with no proof of impact.
- Denial of service by sending a very large volume of traffic.
- Missing security headers or cookie flags in a setup that the documentation does not recommend, such as plain HTTP in production.
- The bundled Grafana, Prometheus, and OpenTelemetry collector. They are for local demos only.

## Your own deployment

You are responsible for the security of the place where you run review. Follow these steps:

1. Serve the Admin UI and the Webhook API over HTTPS, and keep `REVIEW_ADMIN_API_SESSION_COOKIE_SECURE` set to true.
2. Replace every placeholder in `.env`. Use long random values for the secrets.
3. Turn off the GitHub App setup mode (`REVIEW_WEBHOOK_API_SETUP_MODE`) after setup.
4. Keep the database private. Allow connections from the review programs only.
5. Keep review and its container images up to date.
6. If you use OIDC, treat your identity provider as trusted for user names. See the README for the reason.

If you think that a secret leaked, rotate it at once. [`docs/reference/operations.md`](docs/reference/operations.md) explains how to rotate a credential.
