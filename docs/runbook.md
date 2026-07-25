# Operator runbook

Check `docker compose ps`, then `/readyz`, Jenkins `/login`, PostgreSQL
`pg_isready`, and DinD `docker info`. A gateway readiness failure indicates
database connectivity; upstream Jenkins failures appear as HTTP 502 with a
request ID.

Routine work:

- rotate bearer tokens by adding a new digest, restarting gateway, migrating
  clients, then removing the old digest
- rotate GitHub/Jenkins secrets one credential at a time and restart only
  consumers
- review audit, rejected webhook, extension-run, and Jenkins cleanup records
- prune only objects labeled `dev.jenkinsservice.*`
- test restore procedures quarterly

On suspected repository-runtime escape, stop new Jenkins builds, preserve
audit/log evidence, rotate Docker certificates and all credentials available
to trusted orchestration, recreate DinD state, and inspect the host. On write
PAT misuse, revoke the PAT first, disable extensions, then reconcile GitHub
actions from audit records.
