# Threat model

Protected assets are GitHub PATs, Jenkins credentials, Docker client
certificates, webhook and API tokens, source code, artifacts, and audit
records. Principal adversaries are untrusted fork code, compromised
repositories, forged/replayed webhooks, unauthorized MCP clients, malicious
extension output, and an attacker with network access to the host.

Controls include:

- exact-SHA checkout with target-branch trusted orchestration for forks
- no secrets or Docker credentials in repository/extension containers
- HMAC-SHA256 verification and PostgreSQL delivery-ID deduplication
- repository and extension allowlists
- hashed scoped API tokens, constant-time comparison, Origin checks, rate
  limits, HTTPS proxy enforcement, request IDs, and audit records
- curated operations with no raw Jenkins proxy
- output schemas, action capabilities, idempotency, immutable images, resource
  limits, path validation, log redaction, and deterministic cleanup
- networkless repository containers by default, with explicit contract opt-in
  to a uniquely named, cleanup-labeled egress network
- internal networks and loopback-only host bindings

Residual risks include static long-lived MCP tokens, the shared privileged
DinD daemon, automatic write-PAT actions, Jenkins/plugin supply-chain risk,
dependency-download risk in opt-in egress runtimes, and single-host failure.
Rotate all credentials after suspected compromise.
For broadly hosted deployments, replace static tokens with OAuth 2.1
discovery and short-lived tokens.
