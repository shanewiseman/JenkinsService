#!/bin/sh
set -eu

source_directory="${1:?usage: restore.sh BACKUP_DIRECTORY}"
test -f "$source_directory/postgres.dump"
test -f "$source_directory/jenkins-home.tar.gz"
(cd "$source_directory" && sha256sum --check SHA256SUMS)

echo "Restore is intentionally guarded because it replaces persistent state."
echo "Set JENKINS_SERVICE_RESTORE_CONFIRM=replace-persistent-state to continue."
test "${JENKINS_SERVICE_RESTORE_CONFIRM:-}" = "replace-persistent-state"

docker compose stop gateway orchestrator extension-runner jenkins
docker compose exec -T postgres pg_restore \
  --username jenkinsservice \
  --dbname jenkinsservice \
  --clean --if-exists <"$source_directory/postgres.dump"

docker run --rm \
  --volume jenkins-service_jenkins-data:/target \
  --volume "$(cd "$source_directory" && pwd):/backup:ro" \
  alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1 \
  sh -eu -c 'find /target -mindepth 1 -delete; tar -C /target -xzf /backup/jenkins-home.tar.gz'
docker compose up -d
