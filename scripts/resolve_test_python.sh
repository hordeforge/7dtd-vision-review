#!/usr/bin/env bash
# Resolve the interpreter the suite runs under and export it as UV_PYTHON.
#
# It lived inline in the composite action, where `make lint-shell` could not
# reach it. The value it sets decides which interpreter every later `uv run`
# uses, so a resolution that silently yields nothing would collapse the matrix
# onto one version.
set -euo pipefail
cd "$(dirname "$0")/.."

# An explicit request wins; otherwise .python-version is the single home for
# the interpreter a release publishes.
version="${1:-}"
if [ -z "$version" ]; then
    version="$(tr -d '[:space:]' < .python-version)"
fi
if [ -z "$version" ]; then
    echo "ERROR: no interpreter given and .python-version is empty" >&2
    exit 1
fi
echo "UV_PYTHON=$version"
