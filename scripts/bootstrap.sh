#!/bin/sh
set -eu

umask 077
mkdir -p secrets

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "required command not found: $1" >&2
    exit 1
  }
}
require docker
require openssl

create_secret() {
  path="$1"
  bytes="$2"
  if [ ! -s "$path" ]; then
    openssl rand -base64 "$bytes" | tr -d '\n' >"$path"
    chmod 600 "$path"
    echo "created $path"
  fi
}

create_secret secrets/postgres_password 32
create_secret secrets/jenkins_admin_password 24
create_secret secrets/jenkins_api_token 32
create_secret secrets/jenkins_readonly_password 32
create_secret secrets/github_webhook_secret 32
create_secret secrets/build_callback_secret 32
create_secret secrets/review_broker_token 32

if [ ! -s secrets/jenkins_readonly_api_token ]; then
  printf '11%s' "$(openssl rand -hex 16)" >secrets/jenkins_readonly_api_token
  chmod 600 secrets/jenkins_readonly_api_token
  echo "created secrets/jenkins_readonly_api_token"
fi

if [ ! -s secrets/api_tokens ]; then
  api_token="$(openssl rand -hex 32)"
  api_digest="$(printf '%s' "$api_token" | openssl dgst -sha256 -r | awk '{print $1}')"
  printf 'bootstrap-admin|admin|%s\n' "$api_digest" >secrets/api_tokens
  chmod 600 secrets/api_tokens
  echo "created secrets/api_tokens"
  echo "API bearer token (shown once): $api_token"
fi

for pat in secrets/github_read_pat secrets/github_write_pat secrets/openai_api_key; do
  if [ ! -s "$pat" ]; then
    : >"$pat"
    chmod 600 "$pat"
    echo "ACTION REQUIRED: write the operator-managed PAT to $pat"
  fi
done

if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env; set PUBLIC_BASE_URL, allowlists, and origins"
fi

PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://bootstrap.invalid}" \
JENKINS_PUBLIC_URL="${JENKINS_PUBLIC_URL:-https://bootstrap.invalid/jenkins/}" \
  docker compose config --quiet
echo "Compose configuration is valid."
