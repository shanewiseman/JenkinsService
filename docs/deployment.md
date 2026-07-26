# Deployment

## Prerequisites

- x86-64 Linux with Docker Engine 29+ and Compose 5+
- DNS and a Traefik Docker gateway with an HTTPS entrypoint/certificate resolver
- a read-only GitHub fine-grained PAT for metadata/checkout
- a distinct write PAT scoped only to extension-declared actions

Copy `.env.example` to `.env`. `PUBLIC_BASE_URL` must use HTTPS. Set the exact
Traefik DMZ network, entrypoint, certificate-resolver, and hostname names. Keep
Jenkins and API host bindings on loopback; Compose publishes only the Jenkins
and gateway containers to Traefik through the external DMZ network.

Run `scripts/bootstrap.sh`, then add both PATs to their files under `secrets/`.
Secret files are excluded from Git. The `api_tokens` secret contains lines in
this format:

```text
token-id|read,operate|sha256-hex-digest
```

The plaintext token is presented once by the bootstrap script and is never
stored by JenkinsService.

Validate and start:

```bash
docker compose config --quiet
docker compose build
docker compose up -d
curl --fail http://127.0.0.1:18000/readyz
```

Configure the GitHub webhook URL as
`https://your-host/webhooks/github`, content type `application/json`, with the
same HMAC secret in `secrets/github_webhook_secret`.
For GitHub Enterprise, set both `GITHUB_API_URL` and `GITHUB_WEB_URL`; managed
Jenkins checkouts use the latter while extension-requested actions use the
former.

The checked-in reference extension manifest uses a non-routable example image
digest. Before enabling it, publish the image built from
`docker/reference-extension/Dockerfile` to an operator registry and replace
the manifest image with that immutable repository digest, then add
`reference-review` to `EXTENSION_ALLOWLIST`. The default empty allowlist
prevents the unpublished example image from being executed.

For upgrades, back up first, pull the reviewed commit, run Compose
configuration and image validation, build, and use `docker compose up -d`.
Never use a mutable extension or repository runtime image tag.
