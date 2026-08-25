# Changelog

Consumer-visible changes to the `deadeye` CLI and to the contracts other
repositories depend on: the intent schema, the result shape, and the evidence
envelope.

The version has one canonical home, `pyproject.toml`, mirrored by
`src/deadeye/_version.py`. Releases are tag-driven: a `vX.Y.Z` tag that
disagrees with the manifest fails the release instead of publishing.

A change to `schema_version` in the intent file, to the field set of a result,
or to the shape of an evidence envelope is a **breaking change for consumers**
(`7dtd-asset-pipeline`, `7dtd-playtest`) and is called out as such here, not
left to be discovered by a failing parse downstream.

## Unreleased

`pyproject.toml` reads `0.1.0`, but no `v0.1.0` tag exists yet: everything
below is unreleased, and the first tag will close this section rather than
open a new one.

### Added

- `deadeye review CLIP --intent FILE --provider PROVIDER`: forwards a clip
  (a muxed video or a frame sequence) plus the author's recorded intent to a
  vision-capable model and returns one stable, structured result.
- `--allow-network` as an explicit consent gate: no real provider is
  contacted without it.
- `fake` provider: offline by construction, pinning the boundary (exact
  bytes, complete intent) so the whole suite runs with no network and no
  credential.
- `gemini` provider: muxed video inline or a frame sequence.
- `nvidia` provider: NVIDIA NIM vision-chat over a frame sequence.
- Hash-addressed evidence envelopes (`--output`, `--json`): SHA-256 of every
  submitted file and of the intent, the sampling record including what was
  dropped to fit a provider limit, provider and model, rubric and prompt
  versions, the validated result, and tool information with credentials
  removed. A later review never overwrites an earlier envelope without
  `--force`.
- `deadeye doctor` and `deadeye schema`.
- `config.toml` plus a gitignored `config.local.toml` for provider settings
  and credentials, mirroring the sibling llm-proxy convention. Precedence:
  CLI flags > environment > `config.local.toml` > `config.toml` > defaults.
  `deadeye doctor` reports which file a key came from, never its value.
- `ADVISORY_NOTE` on every result: a model critique is evidence, never an
  acceptance.
- `make coverage` and the CI-published coverage badge.
- `SECURITY.md`, documenting the credential boundary, the `--allow-network`
  consent boundary, and what an evidence envelope may contain.
