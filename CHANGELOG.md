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

- The evidence envelope's `provider` block records `elapsed_seconds`, the
  wall-clock time the provider call took, beside the reported token usage.
- Intent documents are bounded locally before anything is submitted: each
  free-text field is capped at 2,000 characters, `avoid`/`questions` at 32
  entries of 500 characters each, and `references` at 8 files. Every field
  lands verbatim in the billable prompt, so a runaway intent is refused with
  a named limit instead of being priced at the provider.
- The gemini adapter sends a `maxOutputTokens` cap on every generation
  (default: the model's published ceiling; override via
  `providers.gemini.max_output_tokens`), so a looping generation cannot bill
  unbounded output.
- The reviewer prompt fences the author's statement between BEGIN/END markers
  declared as authored context data, never instructions (`PROMPT_VERSION`
  moves from `1` to `2`; stored envelopes record which template produced
  them).
- `tests/test_release_contract.py` builds the release sdist and wheel and
  pins their contents, so a file that silently drops out of a release
  artifact fails `make check test` instead of surfacing after the tag.
- The wheel declares PEP 561 typing (`py.typed`) and PEP 639 licensing
  (`License-Expression: MIT`, replacing the deprecated TOML-table license),
  and the build backend floor moves to `setuptools>=77` accordingly.
- The sdist is now a complete source tree (`MANIFEST.in`): the committed
  test suite runs from an unpacked release tarball — previously
  `tests/conftest.py` was omitted, breaking every shipped test — and the
  docs, changelog, security policy, Makefile, bootstrap script, committed
  config files, and locked `uv.lock` resolution ride along.
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

- Evidence envelopes are written as raw bytes instead of text mode, so the
  `evidence.sha256` a review reports always hashes the file's exact contents:
  a platform whose text writes translate newlines to CRLF would otherwise
  strand envelopes whose stored hash disagrees with the file on disk.
- The MCP `review` tool now emits the same stderr disclosure lines as the CLI
  (provider, model, every file and byte about to leave the machine, the
  third-party retention warning) before submitting; previously the transport
  documented disclosure but sent nothing, so an MCP-driven review uploaded
  media unannounced. stdout stays protocol-only.
- A clip whose muxed video exceeds the provider's video byte budget with no
  frames to fall back on is refused with the real fault named ("over the
  provider's N-byte video budget; shorten or recompress the clip") instead of
  falsely claiming the provider cannot ingest video.
- The disclosure line, the evidence's `disclosure.total_bytes`, and the
  `media` list now count a file submitted more than once once per copy: the
  same reference listed twice in an intent is uploaded twice and was
  previously reported by unique path, understating what left the machine.
- `deadeye doctor` no longer describes the credential-less `fake` provider as
  holding a key just because another provider's key is configured.
- The MCP `doctor` tool returns the same `detail` field as
  `deadeye doctor --json`, and the MCP `schema` tool returns exactly what
  `deadeye schema` prints (same surface, same shapes, one shared builder).
- An intent `"schema_version": true` is refused instead of slipping through
  the version check (`True == 1` in Python).
- An intent file saved with a leading UTF-8 BOM (as some editors still write)
  now parses instead of dying as "not valid JSON"; evidence still hashes the
  file's exact bytes, BOM included.
- A Gemini model identifier containing a space or non-ASCII characters is
  percent-encoded into the request URL: it previously went onto the wire as
  raw latin-1 bytes (mojibake) or failed with an opaque encoding error.
- The MCP stdio transport survives a frame carrying an invalid UTF-8 byte: it
  answers the spec's `-32700` parse error and keeps serving, instead of the
  reader raising `UnicodeDecodeError` out of the loop and ending the session.
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
