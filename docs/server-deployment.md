# Production deployment: jenkins.shanewiseman.co

This guide deploys one JenkinsService host with these public endpoints:

| Service | Public URL | Host listener |
| --- | --- | --- |
| Gateway | `https://jenkins.shanewiseman.co` | `127.0.0.1:18000` |
| REST API | `https://jenkins.shanewiseman.co/api/v1` | `127.0.0.1:18000` |
| MCP | `https://jenkins.shanewiseman.co/mcp` | `127.0.0.1:18000` |
| GitHub webhook | `https://jenkins.shanewiseman.co/webhooks/github` | `127.0.0.1:18000` |
| Jenkins | `https://jenkins.shanewiseman.co/jenkins/` | `127.0.0.1:18080` |

Nginx is the only public application listener. PostgreSQL, Docker-in-Docker
(DinD), the Jenkins agent port, the extension runner, and the review broker
must remain unexposed. Do not add host port mappings for them and never mount
the host Docker socket into this stack.

## 1. Prepare the server

Use a dedicated, fully patched x86-64 Linux server. The supported baseline is:

- Docker Engine 29 or newer, with the Compose plugin 5 or newer
- OpenSSL, Nginx, Certbot, and the Certbot Nginx plugin
- a public IPv4 or IPv6 address reachable on TCP 80 and 443
- an `A`/`AAAA` record for `jenkins.shanewiseman.co` pointing to that address
- an SSD-backed filesystem supported by Docker `overlay2`
- enough capacity for Jenkins history, PostgreSQL, artifacts, and the DinD
  image cache; 4 CPU cores, 8 GiB RAM, and 100 GiB free disk is a practical
  starting point, not a substitute for workload-specific sizing

Install Docker from Docker's official repository so the required Engine and
Compose versions are available. On Debian or Ubuntu, install the remaining
packages with:

```bash
sudo apt-get update
sudo apt-get install --yes openssl nginx certbot python3-certbot-nginx
```

Verify the actual server-side versions and storage before continuing:

```bash
docker version --format 'Docker Engine {{.Server.Version}}'
docker compose version
openssl version
nginx -v
certbot --version
docker info --format 'driver={{.Driver}} root={{.DockerRootDir}}'
df -h /var/lib/docker
df -i /var/lib/docker
```

Docker access is root-equivalent. Limit membership in the `docker` group to
administrators of this service. Clone the reviewed JenkinsService commit into
a directory owned by the deployment account, such as
`/srv/jenkinsservice/JenkinsService`, and run all Compose commands from the
repository root.

### Firewall and host exposure

Allow SSH only from the administrative network where possible, plus public
HTTP and HTTPS. For example, when UFW is the host firewall:

```bash
sudo ufw allow from ADMIN_CIDR to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw default deny incoming
sudo ufw enable
sudo ufw status verbose
```

Replace `ADMIN_CIDR` before running the first command. If the provider has a
security group or cloud firewall, enforce the same policy there. Do not allow
TCP 18000, 18080, 2376, 50000, 5432, 8090, or 8100 from another host.
Compose intentionally binds 18000 and 18080 to `127.0.0.1`.

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

Keep the internal `JENKINS_URL`, `DATABASE_URL`,
`EXTENSION_RUNNER_URL`, and secret-file paths from `.env.example`. In
particular, the internal controller URL uses the Compose service name and the
`/jenkins/` prefix; it is not the public URL. Do not put an OpenAI key, GitHub
PAT, Jenkins token, webhook secret, callback secret, or gateway bearer token
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

Use these local checks before configuring Nginx:

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

## 4. Issue the certificate and enable Nginx

Confirm DNS reaches this host before certificate issuance:

```bash
getent ahosts jenkins.shanewiseman.co
```

For the first certificate, enable a minimal HTTP-only Nginx virtual host for
`jenkins.shanewiseman.co`, verify it with `sudo nginx -t`, and request:

```bash
sudo certbot certonly --nginx -d jenkins.shanewiseman.co
```

Certbot must create:

```text
/etc/letsencrypt/live/jenkins.shanewiseman.co/fullchain.pem
/etc/letsencrypt/live/jenkins.shanewiseman.co/privkey.pem
```

After issuance, install the reviewed template and replace the temporary
virtual host:

```bash
sudo install -m 0644 \
  deploy/nginx/jenkins.shanewiseman.co.conf \
  /etc/nginx/sites-available/jenkins.shanewiseman.co
sudo ln -sfn \
  /etc/nginx/sites-available/jenkins.shanewiseman.co \
  /etc/nginx/sites-enabled/jenkins.shanewiseman.co
sudo nginx -t
sudo systemctl reload nginx
```

Disable any default virtual host that conflicts with this hostname. The
template redirects HTTP to HTTPS, forwards `/jenkins/` without stripping its
prefix, and forwards gateway/API/MCP/webhook traffic to port 18000. It also
preserves `Host`, `X-Forwarded-*`, request IDs, streaming, and connection
upgrades.

Verify the public boundary:

```bash
curl --fail --head http://jenkins.shanewiseman.co/jenkins/
curl --fail https://jenkins.shanewiseman.co/healthz
curl --fail https://jenkins.shanewiseman.co/readyz
curl --fail --location \
  https://jenkins.shanewiseman.co/jenkins/login >/dev/null
```

The HTTP request must redirect to HTTPS. Jenkins redirects must remain under
`/jenkins/`; a redirect to `/login` indicates a context-path mismatch.

Enable and test automatic renewal:

```bash
sudo systemctl enable --now certbot.timer
systemctl list-timers certbot.timer
sudo certbot renew --dry-run
```

Keep TCP 80 open so the HTTP-01 renewal challenge can succeed. Monitor the
timer and certificate expiry; do not treat the initial successful issuance as
proof that renewal works.

## 5. Create the GitHub credentials

Create two repository-restricted fine-grained personal access tokens in
GitHub. Select only `shanewiseman/j-link_mcp` under repository access:

1. **Read PAT**: grant **Contents: Read-only**. GitHub grants metadata read
   access automatically. Store it in `secrets/github_read_pat`.
2. **Write PAT**: grant **Pull requests: Read and write** and
   **Commit statuses: Read and write**. Store it in
   `secrets/github_write_pat`.

Use separate token identities if the organization supports that operational
model. Set an expiration, record the owner and rotation date, and approve any
organization authorization GitHub requires. Do not broaden either token to
all repositories. Restart only the services that consume a rotated secret.

## 6. Register j-link_mcp

Read the one-time gateway administrator token from the password manager into
a shell variable without echoing it:

```bash
read -r -s JENKINSSERVICE_ADMIN_TOKEN
```

Register the repository with the trusted default branch `master`:

```bash
curl --fail-with-body \
  --request POST \
  --header "Authorization: Bearer ${JENKINSSERVICE_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --header "Origin: https://jenkins.shanewiseman.co" \
  --data '{"owner":"shanewiseman","name":"j-link_mcp","default_branch":"master","enabled":true}' \
  https://jenkins.shanewiseman.co/api/v1/repositories
```

Save the returned repository UUID. Confirm the registration and request an
initial multibranch scan:

```bash
curl --fail-with-body \
  --header "Authorization: Bearer ${JENKINSSERVICE_ADMIN_TOKEN}" \
  --header "Origin: https://jenkins.shanewiseman.co" \
  https://jenkins.shanewiseman.co/api/v1/repositories

curl --fail-with-body \
  --request POST \
  --header "Authorization: Bearer ${JENKINSSERVICE_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --header "Origin: https://jenkins.shanewiseman.co" \
  --data '{"repository_id":"REPOSITORY_UUID"}' \
  https://jenkins.shanewiseman.co/api/v1/repositories/scan
```

Unset the plaintext shell variable when finished:

```bash
unset JENKINSSERVICE_ADMIN_TOKEN
```

Registration reconciles the managed Jenkins job. Do not create a second
hand-written Jenkins job for the same repository.

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
content, not a blocking result.

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

Monitor `docker compose ps`, `/readyz`, Jenkins queue/agent state, disk and
inode use, certificate expiry, webhook failures, rejected callbacks, review
retries, audit records, and DinD cache growth. Compose log rotation bounds
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
