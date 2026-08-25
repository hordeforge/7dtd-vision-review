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

Put your provider key in the gitignored local config — no `export` needed per
shell:

```bash
cp config.local.toml.example config.local.toml
```

then set `api_key = "nvapi-..."` (or the per-provider form shown in the
example). Verify what deadeye sees:

```bash
deadeye doctor
```

Review a clip directory against the offline fake provider (no network, no
credentials — proves the plumbing end to end):

```bash
deadeye review .local/acceptance/thing/clip \
    --intent assets-src/bundle/thing.review.json \
    --provider fake --allow-network --json
```

Review against a real provider (`--allow-network` is the explicit consent to
upload; every real submission is billable):

```bash
deadeye review .local/acceptance/thing/clip \
    --intent assets-src/bundle/thing.review.json \
    --provider nvidia --allow-network --json
```

Run the vendored end-to-end test — one command that captures a clip **inside
7 Days to Die** through the playtest harness and reviews it against the
configured provider (needs sibling checkouts, a game install, and a dedicated
server; see [docs/e2e.md](docs/e2e.md)):

```bash
scripts/e2e.sh
```

To install the CLI without cloning, every `vX.Y.Z` tag publishes an sdist, a
wheel, and a CycloneDX SBOM on
[GitHub Releases](https://github.com/hordeforge/7dtd-vision-review/releases);
point `uv tool install` at the tagged release's wheel asset:

```bash
uv tool install \
    "https://github.com/hordeforge/7dtd-vision-review/releases/download/vX.Y.Z/7dtd_vision_review-X.Y.Z-py3-none-any.whl"
```

Everything below is detail; the full contract lives in
[docs/reference.md](docs/reference.md).

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
`deadeye prompt --intent FILE [--clip DIR]` renders the exact reviewer prompt
the gateway would inject, without running a review. `deadeye mcp` serves the
same surface as a Model Context Protocol server on stdio (see
[docs/mcp-server.md](docs/mcp-server.md)).

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Disclosure lines go to stderr so a programmatic
caller's stdout stays parseable.

## What a review is

You supply the intent and the clip; the gateway builds the full reviewer
instruction itself (rubric, result shape, your stated purpose and concerns,
and what media actually reached the model — a muxed video, or the sampled
frame sequence with the drop recorded). The prompt is versioned in the
evidence (`rubric_version`, `prompt_version`), so a review is traceable to
the instruction it answered. Verify what a model would be asked before
anything is submitted with `deadeye prompt`.

A review returns `summary`, `strengths`, `recommended_changes`, `limitations`,
`issues` (each tied to a frame range or timestamp), diagnostic `rubric_scores`
(0-5 or null — never pass/fail), and `confidence` (0-1). An `ADVISORY_NOTE`
rides every result: a model critique cannot mark an asset accepted. The full
intent schema and result shape are in [docs/reference.md](docs/reference.md).

Every review is a new billable submission; deadeye never retries one.
Re-running the same command produces an independent envelope with its own
`review_id` — verdicts are not deterministic, and disagreement is preserved,
never averaged.

## Evidence

`--output` writes (and `--json` prints) one hash-addressed envelope: SHA-256
of every submitted frame/clip file and the intent file, the sampling record,
the provider and model with the submission's wall-clock time, rubric and
prompt versions, the validated result, the disclosure confirmation, usage
metadata when reported, and tool/parameter information with credentials
removed. A later review never overwrites an earlier envelope by default. The
envelope's full field set is in [docs/reference.md](docs/reference.md).

## Providers

| Provider | Media it takes | Needs |
|---|---|---|
| `fake` | frames or video | nothing — offline plumbing checks and dry runs |
| `gemini` | muxed video inline or a frame sequence | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| `nvidia` | muxed video (`video_url`) or a frame sequence (NVIDIA NIM) | `NVIDIA_API_KEY` |

Credentials come from the environment or from `config.local.toml`, never as a
command argument, printed output, or stored evidence. `deadeye doctor`
reports state without contacting a provider and names where each key came
from. The provider protocol and adapter details are in
[docs/providers.md](docs/providers.md).

## Configuration

Two TOML files in one directory, loaded in order (`config.local.toml` wins),
mirroring the sibling llm-proxy convention:

- `config.toml` — committed, shared settings: `default_provider`,
  `default_model`, `timeout_seconds`, and per-provider `model` / `endpoint` /
  generation parameters.
- `config.local.toml` — **gitignored**, for your API key and machine-local
  overrides. Copy `config.local.toml.example` to `config.local.toml`.

Precedence: CLI flags > environment variables > `config.local.toml` >
`config.toml` > built-in defaults. Discovery (first directory holding any
config file wins): `DEADEYE_CONFIG_DIR`, then the current directory, then
`~/.config/deadeye/`. A key may be top-level (`api_key = "nvapi-..."`) or
per provider (`[providers.nvidia] api_key = "..."`), with the per-provider
one winning. Values are validated before use — an unknown `default_provider`
is refused, never silently swapped. The full rules are in
[docs/reference.md](docs/reference.md).

## Consuming repositories

`7dtd-asset-pipeline`'s `shamway review-video` and `7dtd-playtest`'s
`scripts/review_video.py` shell out to this CLI: each keeps its own
operation, intent, and evidence documents (adding the fields only it knows —
generation parameters, suite and case) and calls `deadeye` for the model I/O.
See [docs/integration.md](docs/integration.md) for the call contract.

## End-to-end test

`scripts/e2e.sh` captures a real in-game clip through the playtest harness
(no desktop recording) and reviews it against the configured provider, then
writes the evidence under `.local/e2e/`. See [docs/e2e.md](docs/e2e.md).

## Development

```bash
scripts/bootstrap
make check test
```

`make` alone prints the target list; `make all` is everything CI's offline
job runs. While iterating, run one module or one test instead of the whole
suite:

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
