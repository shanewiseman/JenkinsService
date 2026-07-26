#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 OWNER/REPOSITORY [DEFAULT_BRANCH]" >&2
  echo "Example: $0 shanewiseman/j-link_mcp master" >&2
}

if (( $# < 1 || $# > 2 )); then
  usage
  exit 2
fi

repository="$1"
default_branch="${2:-master}"
if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "repository must use the OWNER/REPOSITORY form" >&2
  exit 2
fi
if [[ -z "$default_branch" ]]; then
  echo "default branch must not be empty" >&2
  exit 2
fi

for command_name in curl mktemp python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "required command not found: $command_name" >&2
    exit 1
  fi
done

owner="${repository%%/*}"
name="${repository#*/}"
service_url="${JENKINSSERVICE_URL:-https://jenkins.shanewiseman.co}"
service_url="${service_url%/}"
origin="${JENKINSSERVICE_ORIGIN:-$service_url}"
token="${JENKINSSERVICE_ADMIN_TOKEN:-}"
if [[ -z "$token" ]]; then
  if [[ ! -t 0 ]]; then
    echo "JENKINSSERVICE_ADMIN_TOKEN is required when input is not interactive" >&2
    exit 1
  fi
  read -r -s -p "JenkinsService admin bearer token: " token
  echo >&2
fi
if [[ -z "$token" ]]; then
  echo "JenkinsService admin bearer token must not be empty" >&2
  exit 1
fi
unset JENKINSSERVICE_ADMIN_TOKEN

umask 077
auth_header="$(mktemp "${TMPDIR:-/tmp}/jenkinsservice-auth.XXXXXX")"
cleanup() {
  rm -f -- "$auth_header"
}
trap cleanup EXIT
trap "exit 129" HUP
trap "exit 130" INT
trap "exit 143" TERM
printf "Authorization: Bearer %s\n" "$token" >"$auth_header"
unset token

api_request() {
  local method="$1"
  local path="$2"
  local payload="${3-}"
  local curl_args=(
    --silent
    --show-error
    --fail-with-body
    --request "$method"
    --header "@$auth_header"
    --header "Origin: $origin"
  )
  if [[ -n "$payload" ]]; then
    curl_args+=(
      --header "Content-Type: application/json"
      --data "$payload"
    )
  fi
  curl "${curl_args[@]}" "${service_url}${path}"
}

repositories="$(api_request GET /api/v1/repositories)"
repository_id="$(
  python3 -c 'import json,sys; target=sys.argv[1].casefold(); print(next((str(item["id"]) for item in json.load(sys.stdin) if item.get("full_name", "").casefold() == target), ""))' "$repository" <<<"$repositories"
)"

if [[ -z "$repository_id" ]]; then
  registration_payload="$(
    python3 -c 'import json,sys; print(json.dumps({"owner": sys.argv[1], "name": sys.argv[2], "default_branch": sys.argv[3], "enabled": True}, separators=(",", ":")))' "$owner" "$name" "$default_branch"
  )"
  registration="$(api_request POST /api/v1/repositories "$registration_payload")"
  repository_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$registration")"
else
  update_payload="$(
    python3 -c 'import json,sys; print(json.dumps({"default_branch": sys.argv[1], "enabled": True}, separators=(",", ":")))' "$default_branch"
  )"
  api_request PATCH "/api/v1/repositories/$repository_id" "$update_payload" >/dev/null
fi

scan_payload="$(
  python3 -c 'import json,sys; print(json.dumps({"repository_id": sys.argv[1]}, separators=(",", ":")))' "$repository_id"
)"
api_request POST /api/v1/repositories/scan "$scan_payload" >/dev/null
printf "Activated %s (id=%s, branch=%s)\n" "$repository" "$repository_id" "$default_branch"
