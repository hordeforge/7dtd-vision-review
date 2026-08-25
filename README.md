# 👁️ Deadeye (7DTD Vision Review)

> **Part of [HordeForge](https://github.com/hordeforge)**: High-Performance Systems Engineering for 7 Days to Die.

![CI](https://github.com/hordeforge/7dtd-vision-review/actions/workflows/ci.yml/badge.svg)
![coverage](https://raw.githubusercontent.com/hordeforge/7dtd-vision-review/badges/coverage.svg)
![license](https://img.shields.io/github/license/hordeforge/7dtd-vision-review)
![last commit](https://img.shields.io/github/last-commit/hordeforge/7dtd-vision-review)

**deadeye** reviews game clips with a vision model. You hand it a clip (a
muxed video or a folder of frames) plus an *intent* — what the clip is
supposed to demonstrate — and it returns structured feedback
(`summary`, `issues`, `confidence`, …). The verdict is advisory evidence
for a human call, never an acceptance.

## Quick start

Clone and sync:

```bash
git clone https://github.com/hordeforge/7dtd-vision-review.git
cd 7dtd-vision-review
scripts/bootstrap
```

Put your provider key in the gitignored local config, then check what
deadeye sees:

```bash
cp config.local.toml.example config.local.toml
uv run deadeye doctor
```

Inside a checkout, invoke the CLI through `uv run`: bootstrap builds the
project venv (`.venv`) and installs nothing globally, so plain `deadeye`
is not on your PATH unless you activate that venv or install the wheel
with `uv tool install`.

Review a clip:

```bash
uv run deadeye review CLIP --intent intent.json --provider nvidia --allow-network --json
```

That is the whole interface. `CLIP` is a frames folder or a video file,
`--intent` names the intent JSON, `--allow-network` is your explicit consent
to upload (every real review is billable). Where a clip and an intent come
from is under "How to use"; everything else is detail in
[docs/reference.md](docs/reference.md).

## How to use

### You have a clip already

A clip is a folder of frames or a video file. An intent is a small JSON that
says what the review should judge — one required field, `purpose`:

```json
{"purpose": "the garment must survive a full turn without clipping",
 "avoid": ["clipping", "popping"]}
```

Then:

```bash
uv run deadeye review PATH/TO/CLIP --intent intent.json --provider nvidia --allow-network --json
```

The full intent schema and the result shape are in
[docs/reference.md](docs/reference.md).

### You want the full chain: capture in game, then review

`scripts/e2e.sh` is the one-command end-to-end test: it boots a real client
through the 7dtd-playtest harness, records the fixture's turntable **inside
the game** (the client's own framebuffer, no desktop recording), muxes the
clip, reviews it against your configured provider, and writes the evidence
to `.local/e2e/<timestamp>/`.

```bash
scripts/e2e.sh
```

It finds the game install itself (export `GAME` to override), but needs a
dedicated server — pass `--game-srv DIR` or export
`SEVEN_DAYS_TO_DIE_SERVER_DIR` — plus sibling checkouts of
`7dtd-asset-pipeline`, `7dtd-playtest`, and `7dtd-fastconnect`, and `ffmpeg`
and `uv` on `PATH`. Prerequisites, options, and artifacts:
[docs/e2e.md](docs/e2e.md).

### No game and no network

`--provider fake` reviews without credentials or upload — it proves the CLI
plumbing works offline. `--allow-network` is still required: every review
passes the same consent gate, and nothing leaves the machine for the fake
provider.

```bash
uv run deadeye review CLIP --intent intent.json --provider fake --allow-network --json
```

### From another repository or from code

`7dtd-asset-pipeline`'s `shamway review-video` and `7dtd-playtest`'s
`scripts/review_video.py` shell out to `deadeye review --json`; the exit
code plus the JSON on stdout is the call contract
([docs/integration.md](docs/integration.md)). The same surface is available
as an MCP server via `deadeye mcp`
([docs/mcp-server.md](docs/mcp-server.md)).

### Install without cloning

Every `vX.Y.Z` tag publishes a wheel (and an sdist and SBOM) on
[GitHub Releases](https://github.com/hordeforge/7dtd-vision-review/releases):

```bash
uv tool install \
    "https://github.com/hordeforge/7dtd-vision-review/releases/download/vX.Y.Z/7dtd_vision_review-X.Y.Z-py3-none-any.whl"
```

## Scripts

| Script | What it is |
|---|---|
| `bootstrap` | one-time setup: `uv sync` from the committed lockfile |
| `e2e.sh` | the end-to-end test: in-game capture + review (see above) |
| `coverage_badge.py` | release tooling: render the coverage badge |
| `release_notes.py` | release tooling: draft notes from the changelog |

## Reference

- [docs/reference.md](docs/reference.md) — command reference, intent schema,
  result shape, evidence envelope, configuration rules
- [docs/providers.md](docs/providers.md) — the provider protocol and adapters
- [docs/integration.md](docs/integration.md) — the consumer call contract
- [docs/e2e.md](docs/e2e.md) — the end-to-end test in detail
- [docs/architecture.md](docs/architecture.md) — the boundaries that must not blur
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) — the attack surface

## Development

```bash
make check test
```

`make all` is everything CI's offline job runs; `make coverage` measures the
suite. The suite is fully offline: no network, no credentials, no model.

## Security

Credential handling and the network-consent boundary are in
[SECURITY.md](SECURITY.md).

## License

MIT. This repository owns no art, no mod, and no game content.
