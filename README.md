# 👁️ Deadeye (7DTD Vision Review)

> **Part of [HordeForge](https://github.com/hordeforge)**: High-Performance Systems Engineering for 7 Days to Die.

![CI](https://github.com/hordeforge/7dtd-vision-review/actions/workflows/ci.yml/badge.svg)
![coverage](https://raw.githubusercontent.com/hordeforge/7dtd-vision-review/badges/coverage.svg)
![license](https://img.shields.io/github/license/hordeforge/7dtd-vision-review)
![last commit](https://img.shields.io/github/last-commit/hordeforge/7dtd-vision-review)

**deadeye** is the shared vision-model review gateway for hordeforge. It
forwards a clip (a muxed video or a frame sequence) plus the author's recorded
intent to a configured vision-capable model and returns structured, advisory
feedback in one stable result shape. Consumed programmatically by
`7dtd-asset-pipeline` (`shamway review-video`) and `7dtd-playtest`
(`scripts/review_video.py`), and usable standalone.

A verdict from deadeye is evidence about the submitted media under the
recorded intent. It is **never** an acceptance: human sign-off in the real
game context decides that, in the consuming repository's gates.

## Quick start

Clone and sync (needs [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/hordeforge/7dtd-vision-review.git
cd 7dtd-vision-review
scripts/bootstrap
```

Review a clip directory against the offline fake provider (no network, no
credentials — proves the plumbing end to end):

```bash
deadeye review .local/acceptance/thing/clip \
    --intent assets-src/bundle/thing.review.json \
    --provider fake --allow-network --json
```

Everything below is detail.

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

`deadeye doctor [--json]` reports provider capability state without contacting
any provider. `deadeye schema` prints the intent and result schemas.
`deadeye prompt --intent FILE` renders the exact reviewer prompt the gateway
would inject for that intent, without running a review — the harness for
verifying what a model will be asked before anything is submitted.
`deadeye mcp` serves the same surface as a Model Context Protocol server on
stdio, so an MCP client (an agent, a dashboard) reaches the gateway over
standard JSON-RPC; see [docs/mcp-server.md](docs/mcp-server.md).

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Disclosure lines go to stderr so a programmatic
caller's stdout stays parseable.

## You never write a prompt

The gateway builds the full reviewer instruction from the intent file
automatically: the rubric dimensions, the exact JSON result shape, the
author's stated purpose and concerns, and what media actually reached the
model (a muxed video, or the sampled frame sequence with the drop recorded).
The caller supplies the intent and the clip; nothing else is prompt-shaped by
the caller, and the prompt is versioned in the evidence (`rubric_version`,
`prompt_version`) so a review is traceable to the instruction it answered.
`deadeye prompt --intent FILE [--clip DIR]` renders that instruction for
inspection before submission.

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
provider and model, rubric and prompt versions, the validated result, the
disclosure confirmation, usage metadata when reported, and tool/parameter
information with credentials removed. A later review never overwrites an
earlier envelope by default.

## Providers

| Provider | Media it takes | Needs |
|---|---|---|
| `fake` | frames or video | nothing — offline plumbing checks and dry runs |
| `gemini` | muxed video inline or a frame sequence | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `nvidia` | muxed video (`video_url`) or a frame sequence (NVIDIA NIM) | `NVIDIA_API_KEY` |

Credentials come from the environment or from `config.local.toml` (see
Configuration), never as a command argument, printed output, or stored
evidence. `deadeye doctor` reports state without contacting a provider and
names where each key came from.

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

## Consuming repositories

`7dtd-asset-pipeline`'s `shamway review-video` and `7dtd-playtest`'s
`scripts/review_video.py` shell out to this CLI: each keeps its own operation,
intent, and evidence documents (adding the fields only it knows — generation
parameters, suite and case) and calls `deadeye` for the model I/O. See
[docs/integration.md](docs/integration.md) for the call contract. An MCP
server surface is planned — [docs/mcp-server.md](docs/mcp-server.md) records
the design.

## Development

```bash
scripts/bootstrap
make check test
```

`make` alone prints the target list; `make all` is everything CI's offline
job runs; `make coverage` runs the suite under coverage and prints the
report CI turns into the badge above. While iterating, run one module or
one test instead of the whole suite:

```bash
uv run pytest tests/test_config.py -q   # one module
uv run pytest -k redact -q              # tests whose name matches
```

Contribution rules, the two boundaries that must not blur, and what a new
provider owes are in [CONTRIBUTING.md](CONTRIBUTING.md); consumer-visible
changes are recorded in [CHANGELOG.md](CHANGELOG.md).

The suite is fully offline: the fake provider pins the boundary (exact bytes,
complete intent) without any network, and no test reads a credential.

## Security

Credential handling, the network-consent boundary, and what an evidence
envelope may contain are documented in [SECURITY.md](SECURITY.md); the full
attack surface with ranked threats is modeled in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## License

MIT. This repository owns no art, no mod, and no game content.
