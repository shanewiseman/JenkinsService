#!/bin/sh
set -eu
exec python -m jenkins_service.contract "${1:-.jenkins/pipeline.yaml}"
