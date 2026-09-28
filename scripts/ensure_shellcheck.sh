#!/usr/bin/env bash
# Make shellcheck available on a runner image that ships without it.
#
# `make check` fails without shellcheck under CI, so the macOS matrix leg has
# to have it before the gate runs, and the Makefile cannot install it. The
# install is a no-op where the tool already exists, so the same call is safe on
# the Ubuntu legs, whose image ships shellcheck.
set -euo pipefail

if command -v shellcheck >/dev/null 2>&1; then
    echo "shellcheck already present: $(command -v shellcheck)"
    exit 0
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: shellcheck is not on PATH and this runner has no brew to install it" >&2
    echo "       install it from your system package manager, then re-run the gate" >&2
    exit 1
fi
brew install shellcheck
command -v shellcheck >/dev/null 2>&1
