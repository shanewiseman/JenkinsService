# Deployable Jenkins CI and MCP Service

## Summary

Build JenkinsService from its currently empty repository as a reproducible,
single-host Docker Compose stack. The stack will provide Jenkins LTS,
concurrent containerized pipelines, GitHub webhook handling, a versioned
pipeline contract, a curated REST/MCP control plane, PostgreSQL persistence,
and an out-of-process extension framework.

Use Python 3.12, FastAPI, Pydantic, and the official MCP SDK for the control
plane. Use Jenkins Configuration as Code and immutable plugin/image locks.
Follow the official
[Jenkins Docker/DinD architecture](https://www.jenkins.io/doc/book/installing/docker/)
and expose MCP through standards-compliant
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Runtime and Security Architecture

- Compose services:
  - Custom `jenkins/jenkins:2.568.1-jdk21` controller with zero executors,
    pinned plugins, JCasC, Job DSL, GitHub Branch Source, Docker Pipeline,
    JUnit, Warnings/SARIF, credentials, and authorization plugins.
  - TLS-enabled `docker:29.6.2-dind` sidecar with a persistent image cache and
    no host Docker socket.
  - Containerized Jenkins orchestration agents with configurable concurrent
    executors; repository commands run in fresh, digest-pinned child
    containers.
  - Python API/MCP gateway, PostgreSQL 17, and an isolated extension runner.
- Bind Jenkins and API defaults to loopback ports `18080` and `18000`, avoiding
  currently occupied ports. Require `PUBLIC_BASE_URL` and an operator-managed
  HTTPS reverse proxy for public API/MCP and webhook access.
- Keep Jenkins, PostgreSQL, DinD, and extension-runner networks internal. Do
  not expose the Docker API, PostgreSQL, or Jenkins agent port to the host.
- Store Jenkins admin/API credentials, two fine-grained GitHub PATs, webhook
  HMAC secret, and API bootstrap token as mounted Docker secrets:
  - Read-only PAT for discovery and checkout.
  - Separately scoped write PAT for extension-requested GitHub actions.
  - Never inject the write PAT or Docker client certificates into repository
    runtime or LLM containers.
- Authenticate API/MCP requests with hashed, scoped static bearer tokens
  (`read`, `operate`, `extend`, `admin`), constant-time comparison, audit
  records, Origin validation, rate limits, and HTTPS enforcement at the proxy.
- Verify GitHub webhook HMAC signatures, deduplicate delivery IDs in
  PostgreSQL, reject repositories outside the configured
  organization/repository allowlist, and asynchronously trigger scans/builds.
- For fork PRs, use the target branch's trusted Jenkinsfile while checking out
  the proposed source revision inside the isolated runtime. Withhold every
  secret from fork runtimes.
- Apply CPU, memory, PID, log-size, timeout, and artifact-size limits. Label
  and clean build containers, volumes, and networks after every run.
- Accept the selected concurrent shared-DinD risk: trusted Jenkins
  orchestration code shares one privileged inner daemon. Repository child
  containers receive neither daemon credentials nor host access.

## Pipeline Contract and Published Interfaces

- Publish `ci.jenkinsservice.dev/v1` as JSON Schema plus documentation,
  examples, and a validator. Each repository supplies:
  - A thin root `Jenkinsfile` invoking the pinned
    `jenkins-service-contract@v1` shared library.
  - `.jenkins/pipeline.yaml` containing a digest-pinned runtime image,
    timeouts, environment names, standards/test/custom steps, report paths,
    artifacts, and extension hook references.
  - Step definitions with stable IDs, commands, relative working directories,
    required/warning behavior, and typed reports. Secret values, absolute
    paths, privileged flags, host mounts, and Docker daemon access are
    forbidden.
- Execute the fixed stage sequence: contract validation, clean exact-SHA
  checkout, coding standards, built-in security scan, dependency/SBOM scan,
  repository tests, custom actions, extension hooks, results publication, and
  cleanup.
- Built-in mandatory gates use digest-pinned Gitleaks and Trivy images and
  produce SARIF plus CycloneDX SBOMs. Repository standards and test steps are
  mandatory; additional security/dependency checks and custom actions are
  optional.
- Continue independent checks after failures where safe, then fail the build
  if any required check failed. Publish JUnit, SARIF, coverage metadata,
  artifacts, console logs, and a canonical `PipelineResult` JSON containing
  repository/commit/PR identity, status, timing, checks, exit codes, report
  locations, artifact digests, and extension runs.
- Implement a single versioned service-operation registry that generates
  OpenAPI routes and MCP exposure, preventing undocumented or internal Jenkins
  operations from leaking into MCP.
- Publish these MCP resource families:
  - Capabilities and service version.
  - Contract schema, guide, and examples.
  - Repository registrations and queue items.
  - Builds, stage/check results, progressive logs, and artifact
    metadata/download links.
  - Extension catalog/runs and webhook delivery audit records.
- Publish these MCP tools:
  - `validate_pipeline_contract`
  - `register_repository`, `update_repository`, `unregister_repository`
  - `scan_repository`
  - `trigger_pipeline`, `retry_pipeline`, `cancel_pipeline`
  - `run_extension_action`
- Mirror every service operation under `/api/v1`; read operations become MCP
  resources and mutations become tools. Health, readiness, authentication
  challenges, and the GitHub webhook receiver are transport endpoints rather
  than service operations.
- Do not expose raw Jenkins REST passthrough, script console, plugin
  management, credential access, arbitrary job XML, filesystem access, or
  internal extension-runner APIs.

## Extension and Repository Distribution Design

- Define an out-of-process extension SDK with a versioned manifest,
  input/output JSON Schemas, declared actions, required secret references,
  resource/network limits, compatible contract versions, and immutable
  container image digest.
- Load extensions only from an operator allowlist. The runner passes a
  read-only source snapshot, diff, build results, and metadata to the
  container; it validates bounded structured output before the gateway acts on
  it.
- Ship a provider-neutral reference review extension and mock agent image. It
  generates review findings and requested GitHub actions without containing a
  provider SDK or receiving the GitHub PAT.
- Honor the selected automatic-write policy: the gateway executes any GitHub
  action declared in the extension manifest and permitted by the configured
  PAT without human approval. Record request, extension/image digest, target,
  response, and idempotency key in the audit log.
- Provide a public-quality layout with package metadata, locked dependencies,
  Dockerfiles, Compose, migrations, schemas, shared library, reference
  extension, fixtures, scripts, and release automation.
- Add README, architecture, deployment, pipeline-contract, result-format,
  MCP/API, extension-development, threat-model, operator/runbook,
  backup/restore, testing, troubleshooting, contribution, security, changelog,
  release-checklist, `AGENTS.md`, and canonical `Agent.md` documentation.
- Dogfood the contract with JenkinsService's own Jenkinsfile and manifest.
- Mark package metadata `LicenseRef-Proprietary` and document that releases
  and redistribution are blocked until a license is chosen. The layout and
  release process will be ready, but no public package/image publication will
  occur.

## Test and Acceptance Plan

- Unit-test schemas, contract validation, API scopes, token hashing,
  Jenkins/GitHub clients, webhook signature/deduplication, result
  normalization, audit logging, extension manifests, and output validation.
- Contract-test valid Python, Node, and generic fixtures plus missing gates,
  mutable image tags, invalid paths, malformed reports, forbidden
  secrets/mounts, and incompatible versions.
- Integration-test PostgreSQL migrations, JCasC startup, locked plugins,
  controller zero-executor policy, DinD TLS, agent connectivity, Jenkins
  reconciliation, webhook-to-build flow, progressive logs, artifacts,
  cancellation, retries, and restart recovery.
- Run concurrent pipelines and prove unique workspaces/networks, deterministic
  cleanup, report isolation, and resource-limit enforcement.
- Security-test fork trust behavior, PAT/Docker-certificate absence from
  runtimes, webhook replay/tampering, SSRF and path traversal, log redaction,
  extension output injection, unauthorized MCP scopes, and attempts to call
  unpublished Jenkins APIs.
- End-to-end test a fixture GitHub PR when operator test credentials are
  supplied: webhook receipt, clean clone, all gates, canonical result retrieval
  through REST and MCP, reference review execution, and idempotent GitHub
  action publication.
- Validate Compose configuration, image health checks, clean-host bootstrap,
  backup/restore, SBOM generation, vulnerability scanning, Python distribution
  builds, and OCI image builds on the available x86-64 Docker 29.3.1/Compose
  5.1.1 host.

## Assumptions and Accepted Concerns

- GitHub.com is the default; configurable web/API base URLs retain GitHub
  Enterprise compatibility.
- This is a single-host reference deployment, not a Kubernetes or
  multi-controller design.
- Static MCP tokens are a documented v1 limitation and do not provide the
  OAuth 2.1 discovery/short-lived-token flow recommended for broadly hosted
  MCP services.
- Concurrent shared DinD provides weaker cross-job isolation than per-build
  daemons.
- Automatic extension writes have the full blast radius of the write PAT's
  configured scopes; separate credentials, manifest capability checks, audit
  records, and idempotency reduce but do not remove that risk.
- The repository is distribution-ready but cannot legally be redistributed
  until licensing is resolved.
