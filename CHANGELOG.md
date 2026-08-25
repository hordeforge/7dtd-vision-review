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

- GitHub Releases carry the tagged version's section of this changelog as
  their notes (`scripts/release_notes.py`), so what changed is readable where
  the release is published; a version with no changelog section still
  publishes, with a default note and a warning.
- `tests/test_release_contract.py` pins the consumer-facing contracts (the
  result key set, the evidence envelope's top-level fields, the intent wire
  fields, and the schema/rubric/prompt versions) and the agreement between
  `pyproject.toml` and `src/deadeye/_version.py`, so an accidental change to
  any of them fails `make check test` instead of shipping.
- `deadeye doctor` prints the effective top-level settings
  (`default_provider`, `default_model`, `timeout_seconds`) and says so when a
  `DEADEYE_CONFIG_DIR` names a directory holding no config file, so
  misconfiguration is visible without opening the files.
- `deadeye prompt --intent FILE [--clip DIR]` renders the exact reviewer
  instruction the gateway would inject, without running a review.
- `deadeye mcp` serves the same surface as a Model Context Protocol server on
  stdio (newline-delimited JSON-RPC 2.0), with the same consent gate: the
  `review` tool refuses without an explicit `allow_network`.
- `deadeye review CLIP --intent FILE --provider PROVIDER`: forwards a clip
  (a muxed video or a frame sequence) plus the author's recorded intent to a
  vision-capable model and returns one stable, structured result.
- `--allow-network` as an explicit consent gate: no real provider is
  contacted without it.
- `fake` provider: offline by construction, pinning the boundary (exact
  bytes, complete intent) so the whole suite runs with no network and no
  credential.
- `gemini` provider: muxed video inline or a frame sequence.
- `nvidia` provider: NVIDIA NIM vision-chat, a muxed video inline or a frame
  sequence.
- Hash-addressed evidence envelopes (`--output`, `--json`): SHA-256 of every
  submitted file and of the intent, the sampling record including what was
  dropped to fit a provider limit, provider and model, rubric and prompt
  versions, the validated result, and tool information with credentials
  removed. A later review never overwrites an earlier envelope without
  `--force`. `--keep-raw-response` preserves a redacted copy of the
  provider's raw response inside the envelope.
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
- Property-based fuzz targets (`tests/test_fuzz_parsers.py`, Hypothesis) for
  the two untrusted-input parsers: model output through
  `parse_model_json`/`validate_result`, and intent documents through
  `parse_intent_text` plus the `redact` credentials backstop.

### Fixed

- A configured but unknown `default_provider` is refused with an error naming
  the value and the valid choices, instead of silently sending billable
  reviews to `gemini`.
- `--timeout` and config `timeout_seconds` are validated before any
  submission: zero, negative, non-finite, and non-numeric values are refused
  with one clear message. Previously `--timeout 0` silently read as the
  120-second default, and a bad config value failed opaquely inside the HTTP
  stack.
- The MCP `review` tool now honors `timeout_seconds` from configuration,
  resolving it exactly like the CLI (it previously ignored config and used a
  hardcoded 120).
- Per-provider `endpoint` overrides are validated before submission: https
  only, with plain http accepted solely for a loopback proxy
  (`http://localhost:8080`), so a mistyped override cannot send the provider
  credential in cleartext or fail deep inside urllib.
- Result validation refuses non-finite issue moments: `json.loads` accepts
  `NaN`/`Infinity` literals, and they would survive into evidence JSON no
  strict reader can parse.
- Deeply nested JSON (beyond the interpreter recursion limit) is refused as
  malformed input by both `parse_model_json` and intent parsing, instead of
  escaping as an uncaught `RecursionError`.
- `validate_result` refuses any non-object input up front; a sequence
  holding exactly the seven result key names previously slipped past the
  key-set checks and died on subscripting.
