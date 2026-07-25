# Troubleshooting

`readyz` returns 503: verify the PostgreSQL secret, DNS on the `database`
network, and that migration `0001_initial.sql` ran on the initially empty
volume.

Jenkins has no executor: the controller must remain at zero. Check the
orchestrator node, agent logs, DinD client certificates, and matching shared
workspace mounts.

Child containers cannot find source: confirm the `agent-workspace` volume is
mounted at `/home/jenkins/agent` in both DinD and the orchestrator.

Webhook is rejected: compare the raw-body HMAC secret, verify
`X-Hub-Signature-256`, delivery/event headers, and `GITHUB_ALLOWLIST`.
Repeated delivery IDs are acknowledged without another trigger.

MCP returns 401/403: validate the bearer token, digest entry, scope, HTTPS
proxy header, and optional Origin. Never paste tokens into issue reports.

Extension fails: ensure the manifest exactly matches the runner catalog, the
image is addressable by digest inside DinD, the action is declared, and
output is bounded schema-valid JSON.
