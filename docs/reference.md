# Reference: the deadeye contract

This page is the detail the README's quick start points at. It holds the
command reference, what the gateway does with a clip, the intent schema, the
result shape, the evidence envelope, and the configuration rules — the parts
of the contract a caller or a future editor needs verbatim.

## The command

```bash
deadeye review CLIP --intent FILE --provider PROVIDER [--intent-text JSON] \
    [--model MODEL] [--allow-network] [--json] [--output PATH] \
    [--keep-raw-response] [--force] [--timeout SECONDS]
```

The bare `deadeye` is the installed entry point (`uv tool install`). From a
source checkout, run the same commands through the project venv instead:
`uv run deadeye ...`.

| Flag | Meaning |
|---|---|
| `CLIP` | a clip directory (frames, optional muxed video, optional `client.log`) or a single video/image file |
| `--intent FILE` | the intent JSON committed beside the source (the reproducible route) |
| `--intent-text JSON` | the same information inline; exactly one of the two |
| `--provider` | `fake` (offline) or a configured real provider |
| `--model` | provider model identifier; default per provider |
| `--allow-network` | consent to uploading the media to the provider; required for any real submission |
| `--json` | print the full evidence envelope to stdout |
| `--output PATH` | also write the evidence envelope there; never overwrites an earlier one without `--force` |
| `--keep-raw-response` | preserve a redacted copy of the provider's raw response in evidence |
| `--force` | overwrite an earlier evidence envelope at `--output` |
| `--timeout SECONDS` | budget for the whole submission, response body included, measured on the monotonic clock rather than as a per-socket-read timeout; overrides `timeout_seconds` from configuration |

`deadeye doctor [--json]` reports provider capability state without contacting
any provider. `deadeye schema` prints the intent and result schemas.
`deadeye prompt (--intent FILE | --intent-text JSON) [--clip CLIP]` renders the
exact reviewer prompt the gateway would inject for that intent, without running
a review — the harness for verifying what a model will be asked before anything
is submitted.
`deadeye mcp` serves the same surface as a Model Context Protocol server on
stdio; see [docs/mcp-server.md](mcp-server.md).

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Disclosure lines go to stderr so a programmatic
caller's stdout stays parseable. Usage misuse (argparse) exits 2, an interrupt
(SIGINT) 130, a closed stdout pipe 141. `python -m deadeye` honors the same
exit codes as the console script.

## What the gateway does with a clip

The caller supplies the intent and the clip; nothing else is prompt-shaped by
the caller. The gateway:

1. validates the intent (see below) and hashes its exact bytes;
2. samples the media to the provider's budget — a muxed video goes as one
   upload when the provider accepts video; otherwise the frame sequence is
   sampled down with the drop recorded, never silently. Byte budgets count
   what the wire carries: media goes inline base64, so 3 raw bytes are
   charged as 4, and the reference media and reviewer prompt riding the same
   request count against it too; the disclosure's `total_bytes` stays the
   files' raw sizes. A video that fits its own budget but not the whole
   request falls back to the frame sequence, and the evidence says so;
3. builds the full reviewer instruction from the intent: the rubric
   dimensions, the exact JSON result shape, the author's stated purpose and
   concerns, and what media actually reached the model (a muxed video, or the
   sampled frame sequence with the drop recorded);
4. submits to the configured provider, with `--allow-network` as the only
   path to any network I/O;
5. validates the provider's answer against the shared result schema — a
   silently coerced field would put words into the reviewer's mouth, so a
   provider answer that does not parse is a refusal, never a repair;
6. writes one hash-addressed evidence envelope (see below).

The prompt is versioned in the evidence (`rubric_version`, `prompt_version`)
so a review is traceable to the instruction it answered.

## The intent file

Committed beside the source the clip describes:

```json
{
  "schema_version": 1,
  "purpose": "show the garment survives a full turn without clipping",
  "subject": "thing (worn garment)",
  "camera_path": "turntable",
  "desired_qualities": "proportions and silhouette read right from every side",
  "avoid": ["clipping", "popping", "z-fighting"],
  "references": [{"path": "refs/known-good.png", "purpose": "known-good silhouette"}],
  "questions": ["does the grip read thin through the turn?"],
  "suite": "demo",
  "case": "thing"
}
```

`purpose` is required and never inferred from a filename; everything else is
optional context. The intent's exact bytes are hashed into the evidence
document.

Every free-text field is bounded locally before anything is submitted: the
document itself is refused above 64 KiB at the read, each field is capped at
2,000 characters, `avoid`/`questions` at 32 entries of 500 characters each,
and `references` at 8 files, each with a 2,000-character `path` and a
500-character `purpose`. Every field lands verbatim in the billable
prompt, so a runaway intent is refused with a named limit
instead of being priced at the provider.

The instruction and the author's statement travel in separate roles. The
instruction (role, JSON output contract, rubric, and the declaration that the
user turn is data) is the provider's system instruction; the statement is the
only authored text in the user turn, between the
`-----BEGIN AUTHOR STATEMENT-----` and `-----END AUTHOR STATEMENT-----`
markers. Intent text or a reference path containing one of those markers is
refused at parse time: a marker inside the intent could close the fence early
and let the rest of the statement speak as gateway instructions.
`deadeye prompt` prints both halves as one block, and the evidence envelope
records that same block.

## The result shape

The same family the audio-review pipeline uses, so a caller handling both
review kinds reads one shape:

- `summary`, `strengths`, `recommended_changes`, `limitations`
- `issues` — each `{description, at_seconds?: [start, end], at_frame?: [start, end]}`
- `rubric_scores` — 0-5 or `null` per dimension, diagnostic never pass/fail
- `confidence` — 0-1

`ADVISORY_NOTE` rides every result: a model critique cannot mark an asset
accepted.

## Evidence

`--output` writes (and `--json` prints) one hash-addressed envelope: SHA-256
of every submitted frame/clip file and the intent file, the sampling record
(exactly which frames went, and what was dropped to fit a provider limit), the
provider and model, `created_utc` (RFC 3339 UTC instant with an explicit
offset, never host-local time), `elapsed_seconds` (monotonic duration of the
provider call, not a wall-clock delta), rubric and prompt versions, the
validated result, the disclosure confirmation, usage metadata when reported,
and tool/parameter information with credentials removed. A later review never
overwrites an earlier envelope by default.

The envelope is written through a unique private temporary file in its
destination directory, flushed and `fsync`'d, then atomically replaced into
place. Without `--force` the destination name is occupied with
`O_CREAT|O_EXCL` before that replace, so two concurrent writers cannot both
land on the same path: the first envelope stays, the second is refused. A
stale predictable temporary filename cannot redirect the write through a
symlink, and a failed or interrupted write deletes the temporary file (and
an unused exclusive placeholder) so they cannot accumulate beside the
destination.

A process killed between that reserve and the replace (`SIGKILL`; the
`finally` that cleans up cannot run) leaves an empty placeholder holding no
review at all. Left alone it would refuse every later run with a message
about an earlier review that does not exist. An empty occupant older than 60
seconds is therefore reclaimed and the run converges on the same path; a
fresher one belongs to a live writer and is still refused, and a real
envelope is never reclaimed however old it is. `--force` remains the only
way to overwrite a published review.

## Running a review twice

Every review is a new billable submission to a third party; deadeye itself
never retries one. Re-running the same command therefore sends the media a
second time and produces an independent envelope with its own `review_id` —
verdicts are not deterministic, and disagreement is preserved rather than
averaged. An earlier envelope at `--output` is refused (use `--force` to
replace it deliberately), and that refusal happens before anything is
contacted: a rerun into an occupied path never reaches the provider, so
obeying the guard costs no submission. When a submission times out or the
connection dies before a complete response, the refusal says so explicitly:
the provider may still have completed and billed that attempt server-side,
so resubmitting is a second billable review, not a retry of the first.

If a review completes but its evidence file cannot be written (disk full,
permissions), the failure keeps its `ERROR:` line and non-zero exit while
the full envelope still reaches you — stdout with `--json`, otherwise the
human summary; over MCP it rides the `isError` tool result. A completed,
billable verdict is never discarded to a local write fault, so recovering
it never means submitting the media twice.

The CLI has no key to offer here: a re-run is a new submission by design. The
MCP `review` tool does, through the optional `idempotency_key` argument: a
client that retries its own call with the same key and the same arguments
gets the first attempt's envelope back without submitting anything, for the
life of the server process. See [docs/mcp-server.md](mcp-server.md).

## Providers

| Provider | Media it takes | Needs |
|---|---|---|
| `fake` | frames or video | nothing — offline plumbing checks and dry runs |
| `gemini` | muxed video inline or a frame sequence | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `nvidia` | muxed video (`video_url`) or a frame sequence (NVIDIA NIM) | `NVIDIA_API_KEY` |

Credentials come from the environment or the loaded configuration (normally
the gitignored `config.local.toml`; see Configuration), never as a command
argument, printed output, or stored evidence. `deadeye doctor` reports state
without contacting a provider and names where a key came from — the
environment, or which of the loaded files holds it. The provider protocol and adapter details live
in [docs/providers.md](providers.md).

## Configuration

Two TOML files in one directory, loaded in order (`config.local.toml` wins),
mirroring the sibling llm-proxy convention:

- `config.toml` — committed, shared settings: `default_provider`,
  `default_model` (used when `--model` is omitted, overrides the provider's
  own default), `timeout_seconds`, and per-provider `model` / `endpoint` /
  generation parameters. Model precedence: `--model` flag > `default_model`
  > `[providers.<name>] model` > built-in default.
- `config.local.toml` — **gitignored**, for your API key and machine-local
  overrides. Copy the shipped `config.local.toml.example` (next to the
  installed `deadeye` package, named by `deadeye doctor` when no config is
  found) to `config.local.toml` and set the key; no `export` needed per
  shell.

Command-line options override their configured equivalents. Credentials prefer
the environment, then the merged configuration; all other settings come from
the merged configuration. In that merge, `config.local.toml` wins over
`config.toml`, and built-in defaults apply when no value is configured.
Discovery (first directory holding any config file wins): `DEADEYE_CONFIG_DIR`,
then the current directory, then `$XDG_CONFIG_HOME/deadeye/` when
`XDG_CONFIG_HOME` is set, otherwise `~/Library/Application Support/deadeye/` on
macOS and `~/.config/deadeye/` everywhere else. A key may be top-level
(`api_key = "nvapi-..."`, like llm-proxy) or per provider
(`[providers.nvidia] api_key = "..."`), with the per-provider one winning.
`deadeye doctor` prints which files were loaded and where a credential came
from — the environment, or which of the loaded files holds the key — never
its value. Under `--json` the stdout array is the provider states alone, so a
config that failed to parse is reported as an `ERROR: ...` line on stderr: a
failed parse makes every provider read as `unavailable`, which in the array
alone is indistinguishable from a missing credential.

The merged config is held in memory for the life of the process, and re-read
when its sources change: the directory discovery chose, the
`DEADEYE_CONFIG_DIR` value, and each file's identity, size, and mtime. A
long-lived `deadeye mcp` server therefore picks up an edited
`config.local.toml` (a key that just landed) or a corrected parse error on the
next call, with no restart.

Values are validated before use, not deep inside a submission: a file that
sets a key deadeye does not read (`default_provder`, `providers.geminie`, a
knob misspelled as `max_token`) is refused at load with the offending name,
because a silently ignored setting leaves the built-in default in force while
its author believes the file was honored. The complete key set is
`default_provider`, `default_model`, `timeout_seconds`, a top-level
`api_key`, and per provider `api_key`, `model`, `endpoint` plus that
provider's generation parameters. Beyond that,
`default_provider` must name a known provider (an unknown name is refused,
never silently swapped for another), `timeout_seconds` and `--timeout` must
be positive numbers, and a per-provider `endpoint` override must be an
`https://` URL — plain `http` is accepted only for a loopback proxy such as
`http://localhost:8080`, so no credential ever rides a public wire in
cleartext. The timeout is a whole-call budget: the adapters read the response
in chunks against a `time.monotonic` deadline, so a provider that keeps
trickling bytes cannot hold a billable submission open by resetting a
per-socket-operation timeout, and an NTP step or a manual clock change during
a review cannot shorten or extend it. Per-provider generation parameters (`max_tokens`,
`reasoning_budget`, `temperature`, `top_p`, `max_output_tokens`) follow the
same rule: a value that is present but unusable for its role — a string
where a number belongs, a boolean, a non-finite float such as `nan` — is
refused when a submission starts, with the offending key named, instead of
being quietly replaced by the built-in default. A review whose parameters
differ from the configuration on record is not traceable evidence.
`deadeye doctor` prints the effective top-level settings
(`default_provider`, `default_model`, `timeout_seconds`) and validates every
per-provider `endpoint` override, so a misconfiguration is visible at
diagnosis time without opening the files or starting a review.
