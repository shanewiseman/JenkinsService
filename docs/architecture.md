# Architecture

The stack has seven roles:

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
7. `review-broker` is the only service that receives the OpenAI API key. It
   accepts authenticated internal review requests, calls the Responses API
   with bounded structured output and no tools, and has no host port. The
   gateway independently validates every proposed path and added line before
   publishing one batched GitHub review. The trusted Jenkins checkout creates a
   byte- and time-bounded exact base-to-head diff and includes it in the signed
   completion callback. When a PR reuses an already-passed exact-SHA native
   build, the gateway retrieves the bounded PR diff through the pull-request
   API, verifies the expected base and head identities before and after the
   download, and applies the same line validation. Neither path gives the
   review broker a GitHub credential.

Persisted Jenkins queue and build identifiers are lazily reconciled on
resource reads. This lets a restarted gateway recover build numbers, validate
terminal `PipelineResult` artifacts, serve progressive logs, and cancel either
queued or running work without relying on in-memory watchers.

The gateway owns the Jenkins job hierarchy. Each registered repository is a
folder under `repositories`; ordinary pushes run in a child named for the
branch, while pull requests run in `PR-NUMBER` children. The children retain
independent Jenkins histories but all execute the same controller-managed,
trusted Pipeline wrapper. Repository or PR source revisions therefore select
test input, never Pipeline orchestration. Legacy aggregate jobs are retained
beside the repository folder with a `--legacy` suffix during migration.

Native results are reusable only when both the source SHA and the trusted
target-branch SHA match. A later PR event can therefore publish the already
passed `native-ci` result without rerunning repository code, while still
executing the PR-only AI review. A target-branch contract change invalidates
that cache and causes a normal PR job.

AI-review logs are gateway-generated artifacts persisted with the build
projection in PostgreSQL. The canonical `PipelineResult` references the
extension run and includes the artifact digest and curated download URL.
Jenkins' already-archived native result remains immutable.

The controller and agent share neither the host Docker socket nor host paths.
The agent workspace is a named volume also mounted in DinD so child containers
can see only explicitly bound workspace paths. Repository runtime containers
receive no Docker TLS directory.

The versioned pipeline shared library is an immutable Git repository baked
into the controller image at `/usr/share/jenkins/ref/shared-library`. Jenkins'
local-checkout system property is enabled for this operator-configured
`file://` source; JCasC fixes the only global-library remote to that image path,
pins its default to `v1`, and disables per-job version overrides. Repository
runtime containers cannot mount or select controller filesystem paths.

`registry.py` is the sole publication boundary. It defines each operation's
ID, REST method/path, required scope, and MCP resource/tool classification.
Transport endpoints (health, readiness, authentication challenge, webhook)
remain outside that registry.

The accepted isolation tradeoff is that trusted orchestration for concurrent
jobs shares one inner privileged daemon. Separate credentials, no daemon
access in repository containers, labels, limits, and deterministic cleanup
reduce but do not eliminate cross-job risk.
