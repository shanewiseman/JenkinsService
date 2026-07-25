#!/bin/sh
set -eu

if grep -q 'LicenseRef-Proprietary' pyproject.toml; then
  echo "release blocked: select and document a redistribution license" >&2
  exit 1
fi
echo "license release gate passed"
