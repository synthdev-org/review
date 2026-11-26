# Changelog

All notable changes to this project are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project uses [Semantic Versioning](https://semver.org/). The project is before 1.0, so any release can change behavior.

## [Unreleased]

## [0.1.0] - Unreleased

First release.

### Added

- Review pipeline that reads a pull request and posts review comments.
- Indexing of the repository with AST-aware chunks, embeddings, and a call and import graph.
- Static analysis that runs before any LLM call and gives the review a deterministic base.
- Separate verification step that checks each finding. A model never rates its own output.
- Memory that learns from dismissed findings and developer feedback. A person approves or removes each learning.
- Admin UI with local sign-in and OpenID Connect (OIDC) sign-in.
- Optional product knowledge from a connected knowhow server. Review works fully without it.
- Docker Compose setup for local use.
- Helm chart for Kubernetes. The chart is pre-1.0 and can change.

[Unreleased]: https://github.com/synthdev-org/review/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/synthdev-org/review/releases/tag/v0.1.0
