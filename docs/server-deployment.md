# Production deployment: jenkins.shanewiseman.co

This guide deploys JenkinsService behind an existing Traefik Docker gateway.
The checked-in Compose labels publish these routes:

| Public route | Traefik router | Backend |
| --- | --- | --- |
| `https://jenkins.shanewiseman.co/jenkins/` | `jenkinsservice-jenkins` | `jenkins:8080` |
| All other paths on `jenkins.shanewiseman.co` | `jenkinsservice-gateway` | `gateway:8000` |

The Jenkins router has higher priority and preserves the `/jenkins/` prefix.
The catch-all gateway router serves REST under `/api/v1`, MCP under `/mcp`, the
GitHub webhook under `/webhooks/github`, and health endpoints. Traefik is the
only public application listener. PostgreSQL, Docker-in-Docker (DinD), the
extension runner, and review broker remain unexposed. The Jenkins inbound-agent
TCP listener is disabled; the orchestrator connects internally over WebSocket.
Jenkins and gateway retain loopback host bindings for local diagnostics only.

Both routed services join the operator-managed external Docker network named by
`DMZ_NETWORK`. This label-based configuration assumes Traefik discovers the
containers through the same Docker provider and can join that network. A
Traefik instance on a separate Docker daemon requires an operator-managed
cross-host provider/overlay design; a local bridge network cannot span hosts.

## 1. Prepare the server

Use a dedicated, fully patched x86-64 Linux server. The supported baseline is:

- Docker Engine 29 or newer, with the Compose plugin 5 or newer
- OpenSSL, curl, and Python 3 for bootstrap and repository activation
- an existing Traefik deployment with the Docker provider enabled
- a Traefik HTTPS entrypoint and ACME certificate resolver
- an external Docker network shared with Traefik, normally `dmz_internal`
- a public IPv4 or IPv6 address reachable by Traefik on TCP 80 and 443
- an `A`/`AAAA` record for `jenkins.shanewiseman.co` pointing to Traefik
- an SSD-backed filesystem supported by Docker `overlay2`
- enough capacity for Jenkins history, PostgreSQL, artifacts, and the DinD
  image cache; 4 CPU cores, 8 GiB RAM, and 100 GiB free disk is a practical
  starting point, not a substitute for workload-specific sizing

Install Docker from Docker's official repository so the required Engine and
Compose versions are available. Verify the actual versions, Traefik network,
and storage before continuing:

```bash
docker version --format 'Docker Engine {{.Server.Version}}'
docker compose version
openssl version
docker network inspect dmz_internal >/dev/null
docker info --format 'driver={{.Driver}} root={{.DockerRootDir}}'
df -h /var/lib/docker
df -i /var/lib/docker
```

If the external network does not exist yet, create it once and attach the
Traefik container to it before starting JenkinsService:

```bash
docker network create dmz_internal
docker network connect dmz_internal TRAEFIK_CONTAINER
```

Replace `TRAEFIK_CONTAINER` with the actual container name. Skip the create or
connect command when that state already exists. If `.env` uses a non-default
`DMZ_NETWORK`, substitute that exact value in these commands.

Do not let Compose create a project-scoped substitute; the network is declared
`external` so its resolved name must match Traefik's network exactly.

Docker access is root-equivalent. Limit membership in the `docker` group to
administrators of this service. Clone the reviewed JenkinsService commit into
a directory owned by the deployment account, such as
`/srv/jenkinsservice/JenkinsService`, and run all Compose commands from the
repository root.

### Firewall and host exposure

Allow SSH only from the administrative network where possible. Allow public
HTTP and HTTPS on the Traefik gateway host; when Traefik shares this host, an
example UFW policy is:

```bash
sudo ufw allow from ADMIN_CIDR to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw default deny incoming
sudo ufw enable
sudo ufw status verbose
```

Replace `ADMIN_CIDR` before running the first command. If Traefik is on a
separate gateway host, apply public 80/443 rules there instead. If the provider
has a security group or cloud firewall, enforce the same policy there. Do not
allow TCP 18000, 18080, 2376, 5432, 8090, or 8100 from another host. The
Jenkins TCP agent listener on port 50000 is disabled. Compose intentionally
binds 18000 and 18080 to `127.0.0.1`; Traefik reaches the container ports
through the external DMZ network.

## 2. Bootstrap configuration and secrets

Start from the checked-in environment template, then run the idempotent
bootstrap. Capture the one-time bearer token printed by the first run directly
into an operator password manager:

```bash
cp .env.example .env
./scripts/bootstrap.sh
```

The token is not stored in plaintext by JenkinsService. The
`secrets/api_tokens` file stores only records of the form:

```text
token-id|read,operate|sha256-hex-digest
```

The bootstrap administrator record has `admin` scope. If its printed
plaintext value is lost, create and install a replacement token; it cannot be
recovered from the digest.

Set these exact deployment values in `.env`:

```dotenv
PUBLIC_BASE_URL=https://jenkins.shanewiseman.co
JENKINS_PUBLIC_URL=https://jenkins.shanewiseman.co/jenkins/
JENKINS_HOST=jenkins.shanewiseman.co
DMZ_NETWORK=dmz_internal
TRAEFIK_ENTRYPOINT=websecure
TRAEFIK_CERT_RESOLVER=myresolver
JENKINS_PORT=18080
API_PORT=18000

GITHUB_API_URL=https://api.github.com
GITHUB_WEB_URL=https://github.com
GITHUB_ALLOWLIST=shanewiseman/j-link_mcp
ALLOWED_ORIGINS=https://jenkins.shanewiseman.co
TRUST_PROXY_HEADERS=true
REQUIRE_HTTPS=true

OPENAI_MODEL=gpt-5.6-terra
OPENAI_REASONING_EFFORT=medium
OPENAI_MAX_OUTPUT_TOKENS=4096
OPENAI_TIMEOUT_SECONDS=60
OPENAI_MAX_RETRIES=3
REVIEW_PROMPT_VERSION=v1
REVIEW_BROKER_URL=http://review-broker:8100
BUILD_CALLBACK_MAX_AGE_SECONDS=300

EXTENSION_ALLOWLIST=
RATE_LIMIT_PER_MINUTE=120
```

Set `DMZ_NETWORK`, `TRAEFIK_ENTRYPOINT`, and `TRAEFIK_CERT_RESOLVER` to the
exact names already configured on the Traefik deployment. Keep the internal
`JENKINS_URL`, `DATABASE_URL`, `EXTENSION_RUNNER_URL`, and secret-file paths
from `.env.example`. In particular, the internal controller URL uses the
Compose service name and the `/jenkins/` prefix; it is not the public URL. Do
not put an OpenAI key, GitHub PAT, Jenkins token, webhook secret, callback
secret, or gateway bearer token
in `.env`.

### Secret inventory

Every file below must be owned by the deployment account, have mode `0600`,
contain only its value (a final newline is tolerated), and remain outside
version control:

| File | Consumer and purpose |
| --- | --- |
| `secrets/postgres_password` | PostgreSQL and gateway database access |
| `secrets/jenkins_admin_password` | Initial Jenkins administrator login |
| `secrets/jenkins_api_token` | Jenkins controller/orchestrator API authentication |
| `secrets/api_tokens` | Hashed, scoped gateway bearer tokens |
| `secrets/github_webhook_secret` | GitHub webhook HMAC verification |
| `secrets/github_read_pat` | Jenkins metadata and private checkout only |
| `secrets/github_write_pat` | Gateway status and pull-request review writes only |
| `secrets/openai_api_key` | Review broker only |
| `secrets/review_broker_token` | Gateway-to-broker internal authentication |
| `secrets/build_callback_secret` | Orchestrator-to-gateway completion HMAC |

The bootstrap creates missing generated secrets without overwriting existing
ones. Put the three operator-managed values into their files without printing
them to terminal output:

```bash
install -m 0600 /secure/input/github-read-pat secrets/github_read_pat
install -m 0600 /secure/input/github-write-pat secrets/github_write_pat
install -m 0600 /secure/input/openai-api-key secrets/openai_api_key
chmod 0600 secrets/*
stat -c '%a %U:%G %n' secrets/*
```

The `/secure/input/...` paths are placeholders for an administrator-controlled
secret source; do not create those files in the repository. Every `stat` line
must begin with `600`. The read and write PATs must be distinct. The OpenAI key
is mounted only into `review-broker`; repository code, Jenkins build
containers, the extension runner, and the gateway do not receive it.

Back up the secret directory separately using authenticated encryption and
the organization's secret-management process. Never include it in an
unencrypted filesystem or repository backup.

## 3. Validate, build, and start

Validate resolved Compose configuration before downloading or starting
anything:

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

All services with health checks must become healthy. The orchestrator should
remain running and appear as an online Jenkins agent. Inspect only the
affected service if startup fails:

```bash
docker compose logs --tail=200 SERVICE
```

Use these local checks before validating Traefik routing:

```bash
curl --fail http://127.0.0.1:18000/healthz
curl --fail http://127.0.0.1:18000/readyz
curl --fail --location http://127.0.0.1:18080/jenkins/login >/dev/null
docker compose exec -T postgres \
  pg_isready -U jenkinsservice -d jenkinsservice
docker compose exec -T docker \
  docker --host tcp://localhost:2376 \
  --tlsverify \
  --tlscacert /certs/client/ca.pem \
  --tlscert /certs/client/cert.pem \
  --tlskey /certs/client/key.pem info >/dev/null
docker compose exec -T gateway python -c \
  "import urllib.request; print(urllib.request.urlopen('http://review-broker:8100/healthz', timeout=5).read().decode())"
```

Then confirm the exposure boundary:

```bash
ss -ltn
docker compose port gateway 8000
docker compose port jenkins 8080
```

The two Compose port commands must report loopback addresses. There must be no
host listener for PostgreSQL, DinD TLS, the Jenkins agent port, the extension
runner, or the review broker.

Jenkins is configured by JCasC, not through the setup wizard. Log in at
`/jenkins/` with user `admin` and the value in
`secrets/jenkins_admin_password`. Confirm:

- the controller has zero executors
- the `orchestrator` node is online with the configured executor count
- the shared library is pinned and loaded
- the GitHub checkout credential exists as username/password with username
  `x-access-token`; its password is the read PAT
- Jenkins reports
  `https://jenkins.shanewiseman.co/jenkins/` as its public URL

Do not add the write PAT, OpenAI key, Docker client certificates, webhook
secret, or gateway tokens to repository jobs.

## 4. Enable Traefik routing and TLS

Confirm DNS resolves to the Traefik gateway and that the configured external
network exists:

```bash
getent ahosts jenkins.shanewiseman.co
docker network inspect dmz_internal
```

Use the `DMZ_NETWORK` value from `.env` instead of `dmz_internal` if it was
customized. The Traefik container must appear on that network. Its Docker
provider should use `exposedByDefault=false`; JenkinsService also explicitly
labels every internal service with `traefik.enable=false` and opts in only the
`jenkins` and `gateway` containers. Traefik must already define the entrypoint
and certificate resolver selected by
`TRAEFIK_ENTRYPOINT` and `TRAEFIK_CERT_RESOLVER`. If HTTP-to-HTTPS redirection
is desired, configure it on Traefik's HTTP entrypoint; this stack publishes only
TLS routers, matching the existing gateway pattern.

The Compose labels create:

- `jenkinsservice-jenkins`: host plus exact `/jenkins` or `/jenkins/` prefix,
  priority 200, backend port 8080, and a permanent `/jenkins` to `/jenkins/`
  redirect middleware
- `jenkinsservice-gateway`: host-wide fallback, priority 1, backend port 8000
- explicit service bindings so Traefik routes only to HTTP ports 8080 and 8000
- TLS and the selected ACME certificate resolver on both routers

Reconcile the routed containers after changing `.env` or labels:

```bash
docker compose config --quiet
docker compose up -d --force-recreate jenkins gateway
docker compose ps
```

Inspect Traefik's dashboard or API and confirm both routers and services are
healthy. A 404 normally means the router was not discovered; a 502 normally
means Traefik chose the wrong network or cannot reach the declared backend
port. Verify public routing and prefix preservation:

```bash
curl --fail --location https://jenkins.shanewiseman.co/jenkins >/dev/null
curl --fail https://jenkins.shanewiseman.co/healthz
curl --fail https://jenkins.shanewiseman.co/readyz
curl --fail --location https://jenkins.shanewiseman.co/jenkins/login >/dev/null
```

Jenkins redirects must remain under `/jenkins/`; a redirect to `/login`
indicates a context-path mismatch. Confirm the certificate is issued by the
configured resolver and monitor Traefik's ACME storage, renewal logs, and
certificate expiry. Do not run a separate certificate lifecycle for this
hostname; Traefik owns issuance and renewal.

## 5. Create the GitHub credentials

Create two repository-restricted fine-grained personal access tokens in
GitHub. Select only `shanewiseman/j-link_mcp` under repository access:

1. **Read PAT**: grant **Contents: Read-only**. GitHub grants metadata read
   access automatically. Store it in `secrets/github_read_pat`.
2. **Write PAT**: grant **Pull requests: Read and write** and
   **Commit statuses: Read and write**. Store it in
   `secrets/github_write_pat`. Do not grant this token **Contents** access.

The AI review broker receives no GitHub PAT. It returns bounded review output to
the gateway, and only the gateway uses the write PAT to publish a batched pull
request review and the `jenkinsservice/ai-review` commit status.

Use separate token identities if the organization supports that operational
model. Set an expiration, record the owner and rotation date, and approve any
organization authorization GitHub requires. Do not broaden either token to
all repositories. Restart only the services that consume a rotated secret.

## 6. Activate a repository

Use the generic activation helper with an `OWNER/REPOSITORY` argument and an
optional trusted default branch:

```bash
./scripts/activate-repository.sh shanewiseman/j-link_mcp master
```

The helper prompts without echo for the one-time gateway administrator bearer
token. It lists existing registrations, creates or reconciles the requested
repository with `enabled=true`, captures its UUID, and requests the initial
managed-job reconciliation. It is safe to rerun. The repository must already match
`GITHUB_ALLOWLIST`.

For a non-default deployment URL or browser origin, set
`JENKINSSERVICE_URL` and `JENKINSSERVICE_ORIGIN`. Non-interactive automation
may provide `JENKINSSERVICE_ADMIN_TOKEN` in its protected environment; do not
put the token on the command line or commit it:

```bash
JENKINSSERVICE_URL=https://ci.example.com \
JENKINSSERVICE_ORIGIN=https://ci.example.com \
  ./scripts/activate-repository.sh OWNER/REPOSITORY main
```

Activation reconciles the managed Jenkins job. Do not create a second
hand-written Jenkins job for the same repository.

The resulting Jenkins hierarchy is
`repositories/OWNER--REPOSITORY/BRANCH`. Pushes to any valid branch create or
update that branch's child Pipeline job. Pull requests run under
`PR-NUMBER`, independently of the source branch's push history. On the first
reconciliation after upgrading an older deployment, JenkinsService renames
the former aggregate repository job to `OWNER--REPOSITORY--legacy`, preserves
its Jenkins history, and rewrites stored build lookups to that legacy path.
Quiesce repository builds before performing this one-time reconciliation.

## 7. Configure the GitHub webhook

In `shanewiseman/j-link_mcp`, open **Settings → Webhooks → Add webhook** and
configure:

- Payload URL:
  `https://jenkins.shanewiseman.co/webhooks/github`
- Content type: `application/json`
- Secret: the exact contents of `secrets/github_webhook_secret`
- SSL verification: enabled
- Events: select individual events, then enable **Pushes** and
  **Pull requests**
- Active: enabled

Use GitHub's **Recent Deliveries** view to confirm the ping and subsequent
events receive a successful response. A redelivery with the same delivery ID
must be accepted without triggering duplicate work. A signature mismatch,
unknown repository, or repository outside `GITHUB_ALLOWLIST` must be rejected.

## 8. First PR and required statuses

Open a same-repository pull request into `master` before enabling required
checks. Verify that GitHub receives both status contexts on the PR head SHA:

- `jenkinsservice/native-ci`
- `jenkinsservice/ai-review`

`native-ci` represents deterministic contract validation, standards,
security/dependency scanning, tests, coverage, packaging, and artifact
publication. `ai-review` succeeds when the broker finds no critical issue,
fails for a critical finding, and also fails after bounded OpenAI retries or
invalid structured output. Non-critical AI findings are comments and summary
content, not a blocking result. Non-PR builds record the AI check as `skipped`
in their canonical result and do not publish a successful `ai-review` commit
status. GitHub's legacy commit-status API has no neutral/skipped state, so
omission is the external non-applicable representation.

If a PR event follows a passed branch build for the same source SHA and trusted
target SHA, `native-ci` is republished from that result without rerunning
repository code. The missing PR-only review still executes. A different
trusted target SHA invalidates reuse and queues the normal `PR-NUMBER` job.

After both names have appeared at least once, configure the repository's
branch ruleset or `master` branch-protection rule:

1. require a pull request before merging
2. require status checks to pass
3. add exactly `jenkinsservice/native-ci` and
   `jenkinsservice/ai-review`
4. require the branch to be up to date if that matches the repository's merge
   policy
5. apply the rule to administrators if bypass is not part of the incident
   recovery policy

Do not select similarly named Jenkins job or stage results. The two stable
status contexts above are the contract with GitHub.

## 9. End-to-end acceptance

Complete both a normal PR and a PR from a GitHub fork into `master`. Retain
the PR URLs, head SHAs, Jenkins build URLs, and gateway request IDs as
acceptance evidence.

For each PR, verify:

- GitHub's signed webhook is accepted and the delivery ID appears once in the
  webhook audit
- Jenkins loads the Jenkinsfile, pipeline contract, and orchestration from
  the trusted target branch, while testing the exact proposed head SHA
- `jenkinsservice/native-ci` transitions from pending to its final state and
  publishes JUnit, Cobertura, SARIF, CycloneDX, and package-validation
  artifacts
- `jenkinsservice/ai-review` transitions from pending to its final state
- the review broker publishes one batched review with its summary and any
  diff-validated inline findings
- the canonical build result contains an `ai-review` check and a downloadable
  `artifacts/ai-review.json` log describing the outcome and every GitHub action
  performed or omitted
- a PR opened after an already-passed exact-SHA branch build reuses native
  checks only when its trusted target SHA also matches, then performs the
  previously missing AI review
- retrying delivery or review processing for the same repository, PR, head
  SHA, model, and prompt version does not create a duplicate review
- build containers, networks, and workspaces are removed at completion
- repository and fork-controlled processes receive no OpenAI key, GitHub PAT,
  Jenkins credential, gateway token, webhook/callback secret, Docker socket,
  or Docker client certificate

Verify credential isolation from Compose inspection, pipeline environment
allowlists, audit records, and the absence of secret values in logs. Never
prove isolation by deliberately echoing a real secret. The fork PR is not
accepted if it runs a Jenkinsfile or pipeline contract from the fork, has
access to credentials, or can change the trusted bootstrap/network policy.

Finally, inspect the public build through REST and MCP, confirm the two GitHub
statuses refer to the same head SHA, and merge only after both required
statuses pass.

## 10. Operations, backup, and upgrades

Monitor `docker compose ps`, Traefik router/service health, `/readyz`, Jenkins
queue/agent state, disk and inode use, ACME renewal and certificate expiry,
webhook failures, rejected callbacks, review retries, audit records, and DinD
cache growth. Compose log rotation bounds
container JSON logs, but Jenkins artifacts, PostgreSQL, and Docker volumes
still require capacity alerts.

Before an upgrade, quiesce builds and create the supported application backup:

```bash
./scripts/backup.sh ./backups/jenkinsservice-$(date +%F)
```

Copy that backup and a separately encrypted secret backup off-host. Back up
Jenkins home and PostgreSQL; DinD image cache and agent workspaces are
disposable. Test a restore quarterly by following
[`backup-restore.md`](backup-restore.md).

For an upgrade:

1. back up and record the currently deployed commit and image digests
2. review and fetch the desired commit
3. run `docker compose config --quiet` and the repository's validation suite
4. build immutable images
5. run `docker compose up -d`
6. repeat local/public health, exposure, Jenkins-prefix, webhook, and test-PR
   checks

Never publish this proprietary repository or its images while package
metadata remains `LicenseRef-Proprietary`.
