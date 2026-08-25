# Reference: the deadeye contract

This page is the detail the README's quick start points at. It holds the
command reference, what the gateway does with a clip, the intent schema, the
result shape, the evidence envelope, and the configuration rules — the parts
of the contract a caller or a future editor needs verbatim.

## The command

```bash
deadeye review CLIP --intent FILE --provider PROVIDER [--model MODEL] \
    [--allow-network] [--json] [--output PATH] [--keep-raw-response] \
    [--force] [--timeout SECONDS]
```

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
| `--timeout SECONDS` | provider budget; overrides `timeout_seconds` from configuration |

`deadeye doctor [--json]` reports provider capability state without contacting
any provider. `deadeye schema` prints the intent and result schemas.
`deadeye prompt --intent FILE [--clip DIR]` renders the exact reviewer prompt
the gateway would inject for that intent, without running a review — the
harness for verifying what a model will be asked before anything is submitted.
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
   sampled down with the drop recorded, never silently;
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

Every free-text field is bounded locally before anything is submitted: each
field is capped at 2,000 characters, `avoid`/`questions` at 32 entries of 500
characters each, and `references` at 8 files. Every field lands verbatim in
the billable prompt, so a runaway intent is refused with a named limit
instead of being priced at the provider.

The prompt declares the author statement data-only between
`-----BEGIN AUTHOR STATEMENT-----` and `-----END AUTHOR STATEMENT-----`
markers. Intent text or a reference path containing one of those markers is
refused at parse time: a marker inside the intent could close the fence early
and let the rest of the statement speak as gateway instructions.

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
provider and model with the submission's wall-clock time, rubric and prompt
versions, the validated result, the disclosure confirmation, usage metadata
when reported, and tool/parameter information with credentials removed. A
later review never overwrites an earlier envelope by default.

## Running a review twice

Every review is a new billable submission to a third party; deadeye itself
never retries one. Re-running the same command therefore sends the media a
second time and produces an independent envelope with its own `review_id` —
verdicts are not deterministic, and disagreement is preserved rather than
averaged. An earlier envelope at `--output` is refused (use `--force` to
replace it deliberately). When a submission times out or the connection dies
before a complete response, the refusal says so explicitly: the provider may
still have completed and billed that attempt server-side, so resubmitting is
a second billable review, not a retry of the first.

## Providers

| Provider | Media it takes | Needs |
|---|---|---|
| `fake` | frames or video | nothing — offline plumbing checks and dry runs |
| `gemini` | muxed video inline or a frame sequence | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `nvidia` | muxed video (`video_url`) or a frame sequence (NVIDIA NIM) | `NVIDIA_API_KEY` |

Credentials come from the environment or from `config.local.toml` (see
Configuration), never as a command argument, printed output, or stored
evidence. `deadeye doctor` reports state without contacting a provider and
names where each key came from. The provider protocol and adapter details live
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
  overrides. Copy `config.local.toml.example` to `config.local.toml` and set
  the key; no `export` needed per shell.

Precedence: CLI flags > environment variables > `config.local.toml` >
`config.toml` > built-in defaults. Discovery (first directory holding any
config file wins): `DEADEYE_CONFIG_DIR`, then the current directory, then
`~/.config/deadeye/`. A key may be top-level (`api_key = "nvapi-..."`, like
llm-proxy) or per provider (`[providers.nvidia] api_key = "..."`), with the
per-provider one winning. `deadeye doctor` prints which files were loaded and
where each key came from — never the value.

Values are validated before use, not deep inside a submission:
`default_provider` must name a known provider (an unknown name is refused,
never silently swapped for another), `timeout_seconds` and `--timeout` must
be positive numbers, and a per-provider `endpoint` override must be an
`https://` URL — plain `http` is accepted only for a loopback proxy such as
`http://localhost:8080`, so no credential ever rides a public wire in
cleartext. `deadeye doctor` also prints the effective top-level settings
(`default_provider`, `default_model`, `timeout_seconds`) so a
misconfiguration is visible without opening the files.
