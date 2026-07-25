#!/bin/sh
set -eu
exec python3 -m jenkins_service.contract "${1:-.jenkins/pipeline.yaml}"
