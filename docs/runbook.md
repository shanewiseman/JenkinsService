# Operator runbook

Check `docker compose ps`, then `/readyz`, Jenkins `/login`, PostgreSQL
`pg_isready`, and DinD `docker info`. A gateway readiness failure indicates
database connectivity; upstream Jenkins failures appear as HTTP 502 with a
request ID.

Routine work:

- rotate bearer tokens by adding a new digest, restarting gateway, migrating
  clients, then removing the old digest
- rotate GitHub/Jenkins secrets one credential at a time and restart only
  consumers; rotating `jenkins_readonly_api_token` revokes the previously
  managed direct-read token when Jenkins restarts
- review audit, rejected webhook, extension-run, and Jenkins cleanup records
- prune only objects labeled `dev.jenkinsservice.*`
- test restore procedures quarterly

A `github_webhook_dispatch` audit failure means GitHub authentication and
delivery persistence succeeded but downstream reconciliation did not. Use its
request and delivery IDs to correlate gateway/Jenkins logs, reconcile the
repository job, and redeliver the GitHub event. Build completion returns `503`
when a non-PR AI-review audit artifact cannot be recorded; the Jenkins callback
retries the same signed, idempotent completion until the artifact exists.

On suspected repository-runtime escape, stop new Jenkins builds, preserve
audit/log evidence, rotate Docker certificates and all credentials available
to trusted orchestration, recreate DinD state, and inspect the host. On write
PAT misuse, revoke the PAT first, disable extensions, then reconcile GitHub
actions from audit records.
