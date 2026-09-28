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
left to be discovered by a failing parse downstream. A change that makes a
previously accepted input refuse, or that changes the outcome of a submission
which used to succeed, breaks the same way, whatever contract it lands on.

Every such change goes under `### Breaking`, first in its section, with the
before, the after, and what the caller must do. The remaining subsections keep
the Keep a Changelog order: `Added`, `Changed`, `Fixed`, `Security`. While the
version is 0.x a breaking change may ride a minor bump; the heading is still
required (`CONTRIBUTING.md`, "Changing a contract").

## Unreleased

### Breaking

- A config file that sets a key deadeye does not read is now refused at load,
  where before it was ignored and the built-in default stayed in force. Before:
  a `default_provder` typo, an unknown `[providers.geminie]` table, or a knob
  misspelled as `max_token` left deadeye running, on the built-in default,
  while the file's author believed the setting applied. After: `deadeye review`
  refuses with a non-zero exit naming the offending dotted path, before any
  provider is contacted, and `deadeye doctor` names the fault as
  `config error: ...` (`ERROR: ...` on stderr in `--json`) with its own exit
  code unchanged at 0. To upgrade, correct the name or delete the line, then
  run `deadeye doctor` to confirm the settings that loaded. Every key
  `docs/reference.md` lists still loads unchanged, and a released config is
  unaffected unless it carried a key deadeye never read. No envelope, schema,
  or result change.
- `--timeout` (and the `timeout_seconds` it overrides) is now a budget on the
  whole provider call, where before it was urllib's per-socket-operation
  timeout. Before: a provider trickling a few bytes per read reset the timeout
  on every read, so a review running well past `timeout_seconds` still
  completed. After: a provider call that has not finished by
  `timeout_seconds` is refused with the same "did not answer within Ns"
  message. Raise `--timeout` (or `timeout_seconds`) if a legitimate review of a
  large clip now times out; the default is still 120 seconds.
- The per-request byte budget now counts the reviewer prompt and the reference
  media that ride the same request, not the candidate media alone. Before: a
  submission that fit on the candidate's encoded size was accepted locally and
  refused by the provider after the full upload. After: such a submission falls
  back to the sampled frame sequence, or is refused locally with the byte
  counts named. No consumer edit; a review that used to be submitted as a muxed
  video may now be sampled frames, which its `sampling` record shows.

### Added

- Property-based fuzz targets for the response-body and prompt-flattening
  boundaries: a raw provider body must decode under its declared charset or
  UTF-8 and refuse by name otherwise, a sanitized envelope must re-serialize
  for a strict JSON reader, and a filename must not survive flattening with
  a line separator in it. Internal only; no shipped behavior changes.
- The MCP `review` tool takes an optional `idempotency_key`. A client that
  retries its own call with the same key and the same arguments now receives
  the first attempt's envelope without submitting the media a second time.
  A key reused with different arguments is refused, only completed reviews
  are recorded (a refusal stays retryable), and the ledger holds the most
  recent 128 keys, least recently used evicted. The guarantee is process-
  local: a restarted server is back to one call, one submission. Calls
  without a key behave exactly as before.

### Fixed

- The MCP idempotency ledger is now bounded by retained bytes as well as by
  entry count: at most 32 MiB of envelopes, oldest evicted, whichever bound
  the next entry crosses first. An entry's size is the client's to choose
  (a call with `keep_raw_response` carries a redacted provider payload,
  which the HTTP reader bounds at 8 MiB), so the entry count alone left a
  long-lived server pinning a gigabyte of replayable verdicts. The entry
  just answered is always kept whatever it weighs, so a key the client is
  about to retry still replays instead of billing twice.

### Changed

- The reviewer instruction and the author's statement now travel in separate
  roles. The instruction (role, JSON output contract, rubric, and the
  declaration that the user turn is data) is the provider's system
  instruction, and the statement is the only authored text in the user turn.
  Previously both were concatenated into one user turn, so an intent file sat
  in the same text as the contract it was supposed not to override. `prompt`
  in the evidence envelope, `deadeye prompt`, and the MCP `prompt` tool all
  still print one block with both halves in order, and the result shape is
  unchanged; `prompt_version` is now `3`. Consumers that read only the
  evidence envelope need no edit. `ReviewRequest` in `providers/base.py`
  gains a `system_prompt` field (empty by default) and a `rendered` property,
  which matters to anyone writing an adapter.
- A provider envelope whose verdict object is present but not an object now
  refuses with the same wording from both hosted adapters (`invalid candidate
  'content'` / `invalid choice 'message'`) instead of two separately worded
  refusals. Refusal text only; no result, envelope, or exit-code change.
- `deadeye schema --help` and `deadeye mcp --help` gained the `description` and
  examples every other subcommand already carried. Help text only; no behavior
  or output change.
- A config file that sets a key deadeye does not read is now refused at load
  with the offending name, instead of leaving the built-in default in force
  while its author believes the file applied. A misspelled top-level key, a
  misspelled provider table, and a misspelled per-provider knob are all named
  in the error. Every documented key loads unchanged.
- `deadeye prompt` on a frame clip now renders the same frame-timing note a
  real review sends (`0 = the first submitted frame`), so a preview matches
  the submission it previews. The review prompt itself is unchanged;
  `prompt_version` still reads "2".
- `scripts/e2e.sh` keeps its own shell and calls three new scripts for what it
  used to embed as `python3 -c` bodies and a heredoc:
  `scripts/doctor_query.py` (provider selection, state, credential detail),
  `scripts/playtest_detect.py` (the sibling 7dtd-playtest install queries), and
  `scripts/e2e_report.py` (suite id, clip size, closing summary). The e2e's
  steps, arguments, and output are unchanged.

### Fixed

- The suite no longer lies about the release gate. Four tests in
  `tests/test_config.py` requested a fixture named `_isolated_config` that
  the shared conftest does not define, so they errored at setup instead of
  running: the malformed-config, misspelled-key, and documented-settings
  checks had stopped executing entirely while the suite stayed green around
  them. They name `isolated_config` now. The concurrent evidence-write test
  was also a coin flip: its barrier lined the two writers up but not their
  outcome, so the loser's refusal was sometimes "an earlier review" and
  sometimes "a write in progress". It is split into the staggered case, which
  is deterministic and pins the exact refusal, and the simultaneous case,
  which pins the invariant either message must preserve.
- `scripts/e2e.sh --help` dropped the last line of its own header: the usage
  text was a hardcoded line range, so anything the header grew past it was
  silently cut, and `2  usage error` was already gone. The block now ends at
  the first line that is not part of it.
- The `fake` provider listed a hand-kept subset of the accepted media formats,
  so the offline dry run refused an intent reference in `.webm` or `.mov` that
  clip discovery accepts and both hosted providers would have submitted. It
  now reads the same suffix table as every other adapter.
- A reference attachment that is a muxed video was labelled `reference image`
  in the reviewer prompt. The label now names the media type, so the model is
  told it is looking at a video.
- The home config directory on macOS. `XDG_CONFIG_HOME` is unset there and
  macOS never reads `~/.config`, so the fallback put the config in a dotfile
  directory no macOS tool looks in. It now resolves to
  `~/Library/Application Support/deadeye/` on macOS and `~/.config/deadeye/`
  everywhere else; `$XDG_CONFIG_HOME` still overrides both. `DEADEYE_CONFIG_DIR`
  and a `config.toml` in the working directory are unaffected, so a config
  already at `~/.config/deadeye` has to be moved to be found.
- `scripts/e2e.sh` ran its provider detection and its summary through a bare
  `python3` it never checked for, while the preflight verified only `deadeye`,
  `ffmpeg`, and `uv`. On a host with `uv` but no system `python3` the run died
  naming an unconfigured provider, a diagnosis unrelated to the fault. The
  preflight now requires `python3` and says so.
- A reference's `path` and `purpose` are bounded by the same budgets as the
  rest of the intent (2,000 and 500 characters), instead of by the 64 KiB
  document cap alone, so eight references cannot fill a whole intent document
  with prose billed on every review.
- A non-positive `providers.gemini.max_output_tokens` or
  `providers.nvidia.max_tokens` is now refused with the key named, instead of
  being sent as the request's generation cap.
- The release workflow's test job named its interpreter twice, in two
  places that can disagree: `uv sync --python 3.13` and, through the
  Makefile, `uv run` re-reading `.python-version`. A bump to
  `.python-version` would have had the job sync one interpreter and test
  another, the same divergence already fixed in the CI matrix. The version
  is read from the file and pinned through `UV_PYTHON`, the way the CI job
  does.
- A config edit no longer waits for a restart. The merged config is cached
  process-wide, and the cache never checked whether the files it was built
  from had changed, so a long-lived MCP server kept answering `doctor` and
  `review` from a `config.toml` or `config.local.toml` an operator had since
  edited, and kept reporting a parse fault after the file was fixed. `load()`
  now compares a signature of the source files (the directory discovery
  chose, the explicit-directory override, and each file's identity, size, and
  mtime) and re-reads them only when that signature changes. An unchanged
  config is still served from the cache without a re-parse. The CLI, which
  runs one command per process, is unaffected.
- A review that was submitted and billed but could not write its evidence
  now holds its MCP `idempotency_key`. The ledger recorded only reviews that
  returned an envelope, so a client whose evidence write failed and retried
  under the same key paid for the same media twice; the completed review is
  recorded with its write fault, and a repeat replays that same `isError`
  result and envelope without submitting again. A refusal that happens before
  the submission (missing clip, occupied evidence path, no consent) still
  leaves the key free, and the CLI is unchanged.
- A provider answering `Content-Type: application/json; charset=undefined`
  crashed the review with a bare `UnicodeError` past the adapters' fault
  mapping, after the submission had already been made. A declared charset
  that resolves to a codec raising anything under `UnicodeError` now takes
  the same fall back to UTF-8 as a charset this interpreter does not know,
  and refuses by name if the body decodes under neither.
- `deadeye doctor --json` no longer hides a malformed config. A config that
  fails to parse makes every provider report `unavailable`, so the array on
  stdout was byte-identical to a missing API key; the fault now rides stderr
  under the usual `ERROR: cannot read config file ...` prefix, with stdout
  still the parseable array and the exit code still 0. The human-readable
  `deadeye doctor` output is unchanged. Not a breaking change for consumers:
  the stdout array keeps its shape, so `7dtd-asset-pipeline` and
  `7dtd-playtest` need no edit.
- The CI Python matrix tested one interpreter three times. `uv run` re-read
  `.python-version` and rebuilt `.venv` as 3.13 before the suite started, so
  the 3.11 and 3.12 legs never ran. The job now pins `UV_PYTHON` to its matrix
  entry and the Makefile forwards it, so the supported floor is actually
  exercised.
- The coverage badge step in CI invoked `scripts/coverage_badge.py` with a
  bare `python`, working only because the workflow put `.venv/bin` on PATH
  first. `make badge BADGE=path` is now the one command locally and in CI, and
  the badge's intermediate coverage JSON is written to a temp directory instead
  of the checkout.
- The release build pinned `SOURCE_DATE_EPOCH` but not the rest of the
  runner's environment; the wheel and sdist build now also run under
  `LC_ALL=C`, `TZ=UTC`, and `PYTHONHASHSEED=0`.
- An evidence path left empty by a killed run (the exclusive publish
  placeholder never reaching its atomic replace) no longer refuses every
  later run with a message about an earlier review that was never published.
  An empty occupant older than 60 seconds is reclaimed, so a rerun converges
  on the same path; a placeholder a live writer still holds, and any
  published envelope however old, are refused as before.
- The per-request byte budget now counts the reviewer prompt and the
  reference media that ride the same request, not the candidate media alone
  (see Breaking, above). A muxed video that fits alone but not beside the
  references falls back to the sampled frame sequence instead of overrunning
  the request.
- A raw provider response preserved with `--keep-raw-response` no longer
  writes a bare `NaN`/`Infinity` token (RFC 8259 defines neither) into the
  evidence document, so a strict reader can parse that document back.
- `--timeout` (and the `timeout_seconds` it overrides) now bounds the whole
  provider call instead of one socket read. urllib's `timeout=` is a
  per-operation timeout, so a provider trickling a few bytes per read reset it
  and the billable submission ran indefinitely, which in a long-lived
  `deadeye mcp` server held the call open without bound. The budget is
  enforced on a `time.monotonic` deadline across the response reads, so a
  clock change mid-review cannot shorten or extend it either. A timed-out
  submission still ends as the same "did not answer within Ns" refusal.

## [0.1.1] - 2026-09-20

### Changed

- Dev tooling upkeep only, via dependabot: ruff 0.16.4 to 0.16.6, coverage
  7.x to 7.x (minor), and hypothesis to its next minor in the dev group. No
  CLI, schema, result, or evidence envelope changes, so no consumer impact.
  Patch bump; `make check test smoke` is green unchanged.

## [0.1.0] - 2026-09-11

First tagged release: everything below shipped under `v0.1.0`.

### Added

- Vendored end-to-end test: `scripts/e2e.sh` runs the full chain against a
  real 7 Days to Die client — scaffolds a turntable fixture modlet
  (`shamway`), captures the clip **in game** through `7dtd-playtest`'s
  `StagedClip` support (the client's own framebuffer, never a desktop
  recording), muxes it, and reviews it with `deadeye review` against the
  configured provider, writing evidence under `.local/e2e/`. No hardcoded
  host paths: sibling checkouts, the game install, and the dedicated server
  come from discovery and environment variables. Exit code `0` only on a
  fully reviewed run, so it can gate. Documented in `docs/e2e.md`.
- The README restructures to a quick-start-first shape and the full contract
  moves to the new `docs/reference.md` (command reference, intent schema,
  result shape, evidence envelope, configuration rules).
- The evidence envelope's `provider` block records `elapsed_seconds`, the
  monotonic duration of the provider call, beside the reported token usage.
  `created_utc` is an RFC 3339 UTC instant with an explicit offset, never
  host-local time.
- Intent documents are bounded locally before anything is submitted: the
  file is refused above 64 KiB at the read, each free-text field is capped
  at 2,000 characters, `avoid`/`questions` at 32 entries of 500 characters
  each, and `references` at 8 files. Every field lands verbatim in the
  billable prompt, so a runaway intent is refused with a named limit
  instead of being priced at the provider.
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
  and credentials, mirroring the sibling llm-proxy convention. Command-line
  options override configured equivalents; credentials prefer environment
  variables, then merged configuration, where the local file wins. `deadeye
  doctor` reports the credential source category, never its value.
- `ADVISORY_NOTE` on every result: a model critique is evidence, never an
  acceptance.
- `make coverage` and the CI-published coverage badge.
- `SECURITY.md`, documenting the credential boundary, the `--allow-network`
  consent boundary, and what an evidence envelope may contain.
- Property-based fuzz targets (`tests/test_fuzz_parsers.py`, Hypothesis) for
  the two untrusted-input parsers: model output through
  `parse_model_json`/`validate_result`, and intent documents through
  `load_intent` plus the `redact` credentials backstop.

### Changed

- Dev-group pins for pytest, coverage, and hypothesis are now exact
  (`pytest==9.1.1`, `coverage==7.15.4`, `hypothesis==6.165.10`), matching
  ruff, mypy, and setuptools, so a lock-less install cannot pick a newer
  major than the committed `uv.lock`.
- Config discovery's home fallback follows `XDG_CONFIG_HOME` when that
  variable is set (`$XDG_CONFIG_HOME/deadeye`); an empty or unset value
  still uses `~/.config/deadeye`. A host that relocated its XDG config
  directory is no longer skipped in favour of a hardcoded `~/.config`.

### Fixed

- Two concurrent `deadeye review --output` writers can no longer both pass
  the exists-check and `replace` onto the same path, silently dropping the
  first billed envelope. Without `--force` the destination name is occupied
  with `O_CREAT|O_EXCL` before the atomic replace, so the second writer is
  refused and the first envelope stays.
- Hosted adapters now cap an HTTP error body the same way they already cap
  a success envelope: only the 300-character fault slice is read, then the
  socket is closed. `HTTPError.read()` with no size previously pulled the
  whole 4xx/5xx payload into memory on the long-lived MCP server before
  slicing it.
- The MCP stdio loop refuses a JSON-RPC frame larger than 1 MiB as a parse
  error and discards through the next newline so the session stays aligned.
  A client (or a missing delimiter) can no longer grow the process with one
  unbounded line.
- An evidence write that fails for any reason, not only `OSError`, deletes
  its unique temporary file, and the payload is flushed and `fsync`'d before
  the atomic replace so a crash cannot leave a partial sibling beside the
  destination.
- Intent documents are refused above 64 KiB at the read, before parse, so a
  huge file on the review path cannot fill the process; the existing
  per-field caps still apply to anything that fits.
- `scripts/e2e.sh` names each run directory `<utc-stamp>-<pid>` instead of
  a second-resolution UTC stamp alone, so two invocations started in the
  same second no longer share a capture directory, playtest session name,
  or evidence path.
- `scripts/e2e.sh` reads the clip's byte size with Python's `os.stat`
  instead of GNU `stat -c`, so the size line does not depend on a GNU
  coreutils flag.
- Per-request byte budgets now count the size media reaches the wire as:
  every adapter submits inline base64, where 3 raw bytes become 4, so a
  budget check on raw file bytes waved through submissions (for example an
  18 MiB video against Gemini's published ~20 MB request cap, which base64
  inflates past the limit) that the provider then refused after the full
  upload. The video budget and the per-request total both compare the
  encoded size and name it in their refusals; the disclosure's
  `total_bytes` still reports the files' raw sizes.
- A completed review whose evidence file cannot be written (disk full,
  permissions) no longer discards the billed verdict: the refusal keeps its
  `ERROR:` line and non-zero exit while the full envelope still reaches the
  caller (stdout with `--json` or the human summary on the CLI, the
  `isError` tool result over MCP), so recovering it never means resubmitting
  the same media as a second billable review.
- An occupied `--output` path is refused before anything is contacted, so a
  rerun into existing evidence never reaches the provider; previously the
  guard fired only at write time, after the submission had been paid for.
- An issue naming a moment with only one half of a `start_frame`/`end_frame`
  or `start_seconds`/`end_seconds` pair is refused with the missing partner
  named, instead of the half being silently dropped while the rest of the
  verdict validated.
- A kept raw provider response (`--keep-raw-response`) is now actually
  redacted. The backstop walks JSON mappings, but a raw response arrives as
  one string, so a response whose text parsed as a JSON document passed
  through untouched and credential-named keys inside it rode straight into
  stored evidence despite the "redacted" claim on the refusal line and in the
  docs. JSON-object/array responses are now parsed, redacted, and
  re-serialized; model prose, bare scalars, and broken JSON come back
  byte-identical.
- Per-provider generation parameters (`max_tokens`, `reasoning_budget`,
  `temperature`, `top_p`, `max_output_tokens`) are validated instead of
  silently ignored: a value that is present but unusable — a string where a
  number belongs, a boolean, a non-finite float such as `nan` — now refuses
  the submission with the offending key named, rather than quietly sending
  the built-in default so the request differs from the configuration on
  record. An absent key still falls back to the built-in default.
- `deadeye doctor` validates every per-provider `endpoint` override and
  prints the reason when one is unusable (for example a plain-`http` root on
  a non-loopback host), so the fault surfaces at diagnosis time instead of at
  review start. Doctor still contacts nothing.
- The reviewer prompt's author-statement fence can no longer be escaped by
  the text it fences: an intent field, list entry, reference purpose, or
  reference path containing a `-----BEGIN AUTHOR STATEMENT-----` /
  `-----END AUTHOR STATEMENT-----` marker is refused at parse time, before
  anything is submitted, because such a marker could close the data-only
  fence early and let the rest of the statement speak as gateway
  instructions. Filenames rendered into prompt text (attachment labels, the
  reference listing, media summaries) have control characters flattened, so
  a name carrying a newline cannot forge extra label-shaped lines; evidence
  keeps the true paths.
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
- `validate_result` refuses a model answer whose `strengths`,
  `recommended_changes`, or `limitations` is not an array of strings.
  Those three checks ran after the refusal gate, so the problems they found
  were never raised and a malformed answer silently became empty lists in
  the stored evidence.
- `python -m deadeye` now exits with `main()`'s code instead of always 0: the
  module form previously swallowed every refusal's exit status, so a script
  driving it could read a failed review as success. The console script was
  unaffected.
- An interrupt (Ctrl+C) during a review exits 130 with one stderr line
  (`ERROR: interrupted`) instead of an unhandled traceback, and a downstream
  reader closing the pipe on stdout (`... | head`) exits 141 quietly instead
  of failing in the interpreter's shutdown flush with exit 120.
- `deadeye review --help` gained an examples epilog walking the recommended
  flow: `doctor` first, then `prompt` to see what would be asked, then the
  offline fake review, then a real billable one.
