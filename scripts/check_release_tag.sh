#!/usr/bin/env bash
# Check that the pushed tag names the version this tree declares, in both
# places it is written: pyproject.toml and the src/deadeye/_version.py mirror.
# A wheel must never claim a version nobody tagged, and the mirror must not
# drift from pyproject.toml.
#
# It lived inline in the release workflow, where `make lint-shell` could not
# reach it. The version check is the gate that stands between a mistagged
# push and a published artifact, so it runs here like every other script.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${GITHUB_REF_NAME:?GITHUB_REF_NAME is required: run this from a tag push}"
case "$TAG" in v*) ;; *) echo "ERROR: tag '$TAG' does not start with 'v'" >&2; exit 1;; esac
PYPROJECT="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)"
MIRROR="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/deadeye/_version.py | head -1)"
if [ -z "$PYPROJECT" ]; then
    echo 'ERROR: no version = "..." line found in pyproject.toml' >&2
    exit 1
fi
if [ "$PYPROJECT" != "${TAG#v}" ]; then
    echo "ERROR: tag $TAG but pyproject.toml says version $PYPROJECT; make them match before tagging" >&2
    exit 1
fi
if [ "$MIRROR" != "$PYPROJECT" ]; then
    echo "ERROR: pyproject.toml says $PYPROJECT but src/deadeye/_version.py says $MIRROR" >&2
    exit 1
fi
echo "ok: tag $TAG matches pyproject.toml and _version.py ($PYPROJECT)"
