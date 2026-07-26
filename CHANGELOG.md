# Changelog

All notable changes use Keep a Changelog categories.

## [Unreleased]

### Added

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
