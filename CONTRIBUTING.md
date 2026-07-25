# Contributing

All changes require a pull request, passing tests, updated documentation for
public behavior, and review of security boundaries. Do not weaken immutable
image pins, secret separation, target-branch trust, operation curation,
extension capability checks, or audit/idempotency behavior.

Run Ruff, mypy, pytest, package build, and `docker compose config --quiet`.
Add contract fixtures for validation changes. Never commit `.env`, files
under `secrets/`, real repository data, PATs, Jenkins tokens, or Docker
certificates.
