#!/usr/bin/env bash
# Publish the coverage badge to the `badges` branch, which
# raw.githubusercontent serves to the README.
#
# The badge is the one artifact the coverage job writes, and this script is
# the one place that write happens. It lived inline in the workflow, where no
# linter could reach it: `make lint-shell` covers scripts/ and nothing else,
# so the credential handling below shipped unanalyzed while the shell the
# repository runs every day was shellchecked. A checked script also runs the
# same locally, so the token path can be rehearsed without a push.
#
# Usage: publish_badge.sh <path-to-svg>
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <coverage.svg>" >&2
    exit 2
fi
source_svg="$1"
if [ ! -f "$source_svg" ]; then
    echo "ERROR: $source_svg does not exist; build the badge before publishing it" >&2
    exit 1
fi

# The write-scoped token reaches git through an askpass helper that reads it
# out of the environment, never through a remote URL: a URL form persists into
# badge-repo/.git/config and puts the token in argv of every git process
# (THREAT_MODEL T9). The helper lives in a temp dir and the trap removes it,
# so nothing about this outlives the job.
#
# RUNNER_TEMP is the runner's own scratch dir. A local rehearsal has none, so
# the fallback keeps the script runnable outside CI; mktemp -d rather than a
# fixed path so two runs cannot share an askpass helper.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
askpass="$work/badge-askpass.sh"
# The single quotes are deliberate: $1 and $GITHUB_TOKEN belong to the helper
# git runs, not to this script.
# shellcheck disable=SC2016
printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    '  *[Uu]sername*) echo x-access-token ;;' \
    '  *) printf "%s\n" "$GITHUB_TOKEN" ;;' \
    'esac' > "$askpass"
chmod +x "$askpass"
export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0

url="https://github.com/${GITHUB_REPOSITORY}.git"
if git ls-remote --exit-code --heads origin badges >/dev/null 2>&1; then
    git clone --depth 1 --branch badges "$url" badge-repo
else
    git init -q -b badges badge-repo
    git -C badge-repo remote add origin "$url"
fi
cp "$source_svg" badge-repo/coverage.svg
cd badge-repo
# The clone carries its own origin, and it is already the credential-free URL;
# the fresh repo has just been given the same one. Neither needs rewriting, so
# the token never reaches a remote entry on disk.
git config user.name "hordeforge-ci"
git config user.email "ci@hordeforge.noreply.github.com"
git add coverage.svg
git diff --cached --quiet || git commit -m "update coverage badge"
git push origin badges
