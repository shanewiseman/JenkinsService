#!/bin/sh
set -eu

python="${PYTHON:-python3.12}"
"$python" -m pip install --upgrade pip-tools
"$python" -m piptools compile \
  --resolver=backtracking \
  --strip-extras \
  --output-file=requirements.lock \
  pyproject.toml
"$python" -m piptools compile \
  --resolver=backtracking \
  --extra=dev \
  --all-build-deps \
  --strip-extras \
  --output-file=requirements-dev.lock \
  pyproject.toml
