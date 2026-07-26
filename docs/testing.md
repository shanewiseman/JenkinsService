# Testing

Unit and contract tests exercise schema and semantic validation, scopes and
token hashes, client request boundaries, webhook signatures/replays and
allowlists, result models, audit records, extension manifests/output, and the
operation publication boundary. Regression coverage includes delayed Jenkins
legacy-job rename visibility, concurrent folder-creation collisions, durable
webhook dispatch-failure audits, transient AI-review persistence failures,
callback retry recovery, and resumption of incomplete idempotent review runs.

```bash
pytest --cov=jenkins_service
docker compose config --quiet
python -m build
```

Integration validation uses a disposable Compose project and checks
PostgreSQL migrations, JCasC startup, locked plugins, controller zero
executors, DinD TLS, agent connectivity, webhook-to-build dispatch,
progressive logs, artifacts, cancellation/retry, and restart recovery.

Security acceptance should additionally run fork PRs, webhook tamper/replay,
path traversal, secret and Docker-certificate absence, unauthorized MCP
scopes, extension output injection, and unpublished Jenkins API attempts.
Operator-supplied fixture credentials enable the end-to-end GitHub PR test;
tests skip it when those credentials are absent.
