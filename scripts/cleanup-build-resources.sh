#!/bin/sh
set -eu

label="${1:?usage: cleanup-build-resources.sh BUILD_LABEL}"
case "$label" in
  dev.jenkinsservice.build=*) ;;
  *) echo "refusing unexpected cleanup label" >&2; exit 2 ;;
esac

docker ps -aq --filter "label=$label" | xargs -r docker rm -f
docker network ls -q --filter "label=$label" | xargs -r docker network rm
docker volume ls -q --filter "label=$label" | xargs -r docker volume rm
