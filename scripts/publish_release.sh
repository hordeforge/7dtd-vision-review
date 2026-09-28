#!/usr/bin/env bash
# Create the GitHub Release for the pushed tag and upload the built assets.
#
# It lived inline in the release workflow, where `make lint-shell` could not
# reach it. It is the step that puts a write token and a published artifact on
# the same code path, so it is analyzed like every other script in the tree.
#
# Expects `make dist` to have run: dist/ carries the wheel, the sdist, the
# SBOM, and the SHA256SUMS manifest.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${GITHUB_REF_NAME:?GITHUB_REF_NAME is required: run this from a tag push}"
VERSION="${TAG#v}"
notes="${RUNNER_TEMP:-$(mktemp -d)}/release-notes.md"

# The release notes are the changelog section for this version, so consumers
# read what changed in the release itself and a version with no changelog
# entry is visible instead of silently generic.
read -r -d '' DEFAULT_NOTES <<EOF || true
HordeForge Release ${TAG}: shared vision-model review gateway returning structured, hash-addressed, advisory evidence on a clip under its recorded intent.

See CHANGELOG.md in the tagged tree for the consumer-visible changes.
EOF
if python3 scripts/release_notes.py "$VERSION" > "$notes"; then
    echo "release notes: from the CHANGELOG.md ${VERSION} section"
else
    code=$?
    if [ "$code" -ne 3 ]; then
        exit "$code"
    fi
    echo "WARNING: no CHANGELOG.md section for ${VERSION}; using the default note. Rename '## Unreleased' to '## ${VERSION}' before tagging." >&2
    printf '%s\n' "$DEFAULT_NOTES" > "$notes"
fi

# Convergent on re-run: a failed attempt that already created the release
# (assets missing or partial) must not wedge the next run on "already
# exists". Create only when absent, then always upload with --clobber so both
# paths end with the same complete set.
#
# Convergent is not the same as silent. An asset the release already carries
# must be byte-identical to the file about to replace it, so a re-run after a
# partial failure still finishes while a re-pushed tag that would swap the
# published bytes for a different build stops here instead (THREAT_MODEL
# T10). The build is reproducible, so the same tag and tree can only disagree
# for a real reason.
if gh release view "$TAG" --json id -q .id >/dev/null 2>&1; then
    echo "release $TAG already exists; replacing its assets"
    published="$(mktemp -d)"
    trap 'rm -rf "$published"' EXIT
    differs=0
    for asset in dist/*.whl dist/*.tar.gz dist/sbom.cdx.json dist/SHA256SUMS; do
        name="$(basename "$asset")"
        # A pattern that matches nothing exits non-zero: the asset was never
        # published, which is the partial-release case this step exists to
        # finish, not a conflict.
        if gh release download "$TAG" --pattern "$name" --dir "$published" >/dev/null 2>&1 \
            && ! cmp -s "$asset" "$published/$name"; then
            echo "ERROR: published asset $name differs from what this tag builds." >&2
            echo "       Consumers have already downloaded it; delete the" >&2
            echo "       release and re-tag to publish the new bytes." >&2
            differs=1
        fi
    done
    if [ "$differs" -ne 0 ]; then
        exit 1
    fi
else
    gh release create "$TAG" \
        --title "Deadeye / Vision Review ${VERSION}" \
        --notes-file "$notes"
fi
gh release upload "$TAG" dist/*.whl dist/*.tar.gz dist/sbom.cdx.json dist/SHA256SUMS --clobber
