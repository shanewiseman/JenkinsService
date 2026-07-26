# REST and MCP

All service operations are declared in `registry.py` and mirrored under
`/api/v1`. Read operations are MCP resources; mutations are MCP tools. MCP
uses standards-compliant Streamable HTTP at `/mcp`.

Bearer tokens have `read`, `operate`, `extend`, or `admin` scope. `operate`
implies `read`; `extend` implies `read`; `admin` implies all scopes. Token
digests are loaded from a Docker secret and compared in constant time.

Published tools are:

- `validate_pipeline_contract`
- `register_repository`, `update_repository`, `unregister_repository`
- `scan_repository`
- `trigger_pipeline`, `retry_pipeline`, `cancel_pipeline`
- `run_extension_action`

`trigger_pipeline` accepts an optional `branch`. Omitting it selects the
repository's trusted default branch; supplying it routes a non-PR execution
to that branch's independent Jenkins job. A `pull_request` routes the
execution to `PR-NUMBER` while retaining `branch` as source-branch metadata.

Resources cover capabilities/version, schemas/examples, repository
registrations, queue items, builds/checks/logs/artifacts, extension catalog
and runs, webhook deliveries, and admin audit records.

Each completed build exposes its structured review artifact at
`/api/v1/builds/{build_id}/artifacts/ai-review.json` and
`jenkinsservice://builds/{build_id}/artifacts/ai-review`. The artifact exists
for passed, failed, and skipped review outcomes and states which GitHub actions
were performed or intentionally omitted.

Health (`/healthz`), readiness (`/readyz`), and GitHub webhook reception are
transport endpoints, not service operations. Requests with an `Origin` must
match the configured allowlist. Public requests require HTTPS as reported by
the trusted proxy. Responses include a request ID and security headers.
