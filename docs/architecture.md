# Architecture

The stack has six roles:

1. `jenkins` is an immutable Jenkins LTS controller configured by JCasC with
   zero executors.
2. `orchestrator` agents run trusted shared-library code with configurable
   concurrency.
3. `docker` is a TLS-enabled privileged DinD sidecar. It has no host socket or
   host port and keeps an inner image cache.
4. `gateway` owns the operation registry, REST routes, MCP resources/tools,
   authentication, audit records, GitHub webhook verification, and upstream
   Jenkins/GitHub clients.
5. `postgres` persists registrations, build/queue projections, webhook
   delivery IDs, extension runs, and the audit log.
6. `extension-runner` launches only operator-allowlisted, immutable extension
   images with no network, a read-only root, dropped capabilities, resource
   limits, bounded JSON input/output, and no GitHub PAT.

Persisted Jenkins queue and build identifiers are lazily reconciled on
resource reads. This lets a restarted gateway recover build numbers, validate
terminal `PipelineResult` artifacts, serve progressive logs, and cancel either
queued or running work without relying on in-memory watchers.

The controller and agent share neither the host Docker socket nor host paths.
The agent workspace is a named volume also mounted in DinD so child containers
can see only explicitly bound workspace paths. Repository runtime containers
receive no Docker TLS directory.

`registry.py` is the sole publication boundary. It defines each operation's
ID, REST method/path, required scope, and MCP resource/tool classification.
Transport endpoints (health, readiness, authentication challenge, webhook)
remain outside that registry.

The accepted isolation tradeoff is that trusted orchestration for concurrent
jobs shares one inner privileged daemon. Separate credentials, no daemon
access in repository containers, labels, limits, and deterministic cleanup
reduce but do not eliminate cross-job risk.
