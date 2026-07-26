# JenkinsService

JenkinsService is a reproducible, single-host Jenkins CI stack with a curated
REST and Model Context Protocol (MCP) control plane. It runs Jenkins jobs in
containerized orchestration agents, executes repository commands in fresh
digest-pinned child containers, persists control-plane state in PostgreSQL,
and isolates optional review extensions behind a bounded runner.

The repository implements the `ci.jenkinsservice.dev/v1` pipeline contract.
Raw Jenkins REST passthrough, the script console, plugin and credential
management, arbitrary job XML, and filesystem access are deliberately absent
from both REST and MCP.

> **License status:** package metadata is `LicenseRef-Proprietary`. Building
> and testing for internal evaluation is supported, but releases,
> redistribution, and public image/package publication are blocked until a
> project license is selected.

## Quick start

Requirements are Docker Engine 29+, Compose 5+, OpenSSL, and an existing
Traefik Docker gateway attached to the configured external DMZ network.

```bash
cp .env.example .env
./scripts/bootstrap.sh
docker compose config --quiet
docker compose build
docker compose up -d
```

Set `PUBLIC_BASE_URL`, `JENKINS_HOST`, `DMZ_NETWORK`,
`TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERT_RESOLVER`, `GITHUB_ALLOWLIST`, and
`ALLOWED_ORIGINS` in `.env` before startup. Place the two fine-grained GitHub
PATs in `secrets/github_read_pat` and `secrets/github_write_pat`; they must be
different credentials. The bootstrap script creates other missing secrets
without replacing existing values and prints the new API bearer token once.

The services bind only to loopback by default:

- Jenkins: `http://127.0.0.1:18080`
- REST and Streamable HTTP MCP: `http://127.0.0.1:18000`
- REST base: `/api/v1`
- MCP transport: `/mcp`
- GitHub webhook receiver: `/webhooks/github`

Compose labels publish Jenkins `/jenkins/` and the gateway host routes through
Traefik with TLS; every internal service is explicitly opted out. Traefik
preserves the host and sets forwarded HTTPS headers.
PostgreSQL, the Docker API, the extension runner, review broker, and Jenkins
agent port have no public host bindings.

Activate any allowlisted repository with its trusted default branch. The helper
prompts for the gateway administrator bearer token and reconciles the initial
repository folder and default-branch job:

```bash
./scripts/activate-repository.sh OWNER/REPOSITORY [DEFAULT_BRANCH]
```

## Repository contract

Consumer repositories include a thin root `Jenkinsfile`:

```groovy
@Library('jenkins-service-contract@v1') _
jenkinsServicePipeline(
    repositoryUrl: 'https://github.com/example/project.git',
    trustedBranch: 'master',
    repository: 'example/project'
)
```

They also provide `.jenkins/pipeline.yaml`; see
[`examples/pipeline.yaml`](examples/pipeline.yaml). Validate it with:

```bash
python -m jenkins_service.contract .jenkins/pipeline.yaml
```

For pull requests, managed Jenkins jobs load orchestration and the contract
from the trusted target branch, then check out the proposed exact SHA only as
untrusted source input. Fork runtimes receive no Jenkins, GitHub, MCP, or
Docker credentials.

Jenkins organizes managed executions as
`repositories/OWNER--REPOSITORY/BRANCH`. Every pushed branch has its own job
and history. Pull-request events use stable `PR-NUMBER` child jobs, so a branch
with an open PR has an independent branch-push history and PR-validation
history. Branch child jobs use bounded, collision-resistant internal names
and show the original branch name in the Jenkins UI.

Every completed build exposes a structured `ai-review.json` artifact. A
non-PR review is recorded as skipped and does not publish a false-success
GitHub status. When a later PR matches both an already-passed source SHA and
the trusted target SHA, JenkinsService reuses the native result and executes
only the missing PR review.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest --cov=jenkins_service
```

The test suite defaults to the in-memory store. PostgreSQL and Compose
integration tests are marked separately and described in
[`docs/testing.md`](docs/testing.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Pipeline contract](docs/pipeline-contract.md)
- [Pipeline result](docs/result-format.md)
- [REST and MCP](docs/mcp-api.md)
- [Extension development](docs/extensions.md)
- [Threat model](docs/threat-model.md)
- [Operator runbook](docs/runbook.md)
- [Backup and restore](docs/backup-restore.md)
- [Testing](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release checklist](docs/release-checklist.md)
