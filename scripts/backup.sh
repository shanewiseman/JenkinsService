#!/bin/sh
set -eu

destination="${1:?usage: backup.sh DESTINATION_DIRECTORY}"
case "$destination" in
  /|"$HOME"|"$HOME/"|.) echo "refusing broad backup destination: $destination" >&2; exit 2 ;;
esac
mkdir -p "$destination"

docker compose exec -T postgres pg_dump \
  --username jenkinsservice \
  --dbname jenkinsservice \
  --format custom >"$destination/postgres.dump"

docker run --rm \
  --volume jenkins-service_jenkins-data:/source:ro \
  --volume "$(cd "$destination" && pwd):/backup" \
  alpine:3.22.1 \
  tar -C /source -czf /backup/jenkins-home.tar.gz .

sha256sum "$destination/postgres.dump" "$destination/jenkins-home.tar.gz" \
  >"$destination/SHA256SUMS"
echo "backup written to $destination"
