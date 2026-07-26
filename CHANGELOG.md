# Changelog

All notable changes use Keep a Changelog categories.

## [Unreleased]

### Fixed

- First-push Jenkins hierarchy reconciliation now serializes per repository,
  waits for legacy rename visibility, and recovers when another reconciler
  wins folder creation; failed asynchronous webhook dispatches leave a
  request-correlated audit record instead of disappearing silently.
- Non-PR completion callbacks now record their skipped/disabled AI-review
  check and log before returning success. AI-review processing retries
  transient failures and resumes idempotent runs left incomplete.

### Added

- Trusted pipeline contracts can exclude exact generated paths from the
  bounded AI-review diff without excluding them from checkout, tests, security
  scans, dependency validation, SBOMs, or artifacts.
- Durable `ai-review.json` build artifacts for passed, failed, and skipped
  reviews, plus exact source/trusted-SHA native result reuse that runs a
  previously missing PR-only review without rerunning repository checks.
- Repository → branch Jenkins job hierarchy with independent histories for
  every pushed branch and stable `PR-NUMBER` jobs, including preservation of
  aggregate pre-upgrade history under `--legacy`.
- Reproducible Jenkins LTS, TLS DinD, orchestration agent, PostgreSQL,
  gateway, and isolated extension-runner Compose stack.
- Versioned pipeline/result/extension schemas and fixed shared-library stages.
- Curated scoped REST and Streamable HTTP MCP operation registry.
- Signed, deduplicated GitHub webhook handling and automatic allowlisted
  extension actions with audit/idempotency.
