# End-to-end test

`scripts/e2e.sh` is the vendored end-to-end test for deadeye: one command
that captures a clip **inside 7 Days to Die** and reviews it against a real
vision provider. It is the full chain — asset-pipeline scaffold, playtest
in-game capture, deadeye review — with no synthetic media and no desktop or
screen recording anywhere: every frame is the client process's own
`ScreenCapture` framebuffer (the `CaseDef.StagedClip` support in
`7dtd-playtest`), supersized and muxed by `scripts/capture_video.sh` there.

Run it from this repository:

```bash
scripts/e2e.sh
```

The provider is chosen automatically: `default_provider` from `config.toml`
when that provider has a key, otherwise the first configured provider; put
the key in `config.local.toml` first (see the README quick start), or pass
`--provider` explicitly. The run takes a few minutes: it scaffolds a fixture
modlet, boots a stock dedicated server plus a real client through
7dtd-playtest, captures a 12-second turntable clip in game, muxes it, and
submits the video to the provider.

## What it validates

- the configured provider's credential is present (`deadeye doctor`),
  before any network use;
- the game client install is found — 7dtd-playtest's own Steam-library scan
  resolves it (export `GAME` to override), and the Proton prefix is derived
  from it;
- a stock dedicated server is found (`SEVEN_DAYS_TO_DIE_SERVER_DIR` or
  `--game-srv`, with a Steam-library scan as a fallback);
- the fixture modlet builds (`shamway` from a sibling
  `7dtd-asset-pipeline` checkout) and its turntable case is a
  `CaseDef.StagedClip`;
- the harness and fastconnect pair are deployed into the client's `Mods`
  behind the shared client lock;
- the in-game capture completes with the expected frame count and muxes;
- the review returns a valid envelope and the evidence lands in `.local/`.

The script exits non-zero on any failure, so it can run as a gate.

## Prerequisites

- sibling checkouts of `7dtd-asset-pipeline`, `7dtd-playtest`, and
  `7dtd-fastconnect` (discovered relative to this repository; override with
  `ASSET_PIPELINE_ROOT`, `PLAYTEST_ROOT`, `CONNECT_ROOT`);
- a Steam install of 7 Days to Die with the playtest harness built (the
  script builds it on first use) and a stock dedicated server;
- `ffmpeg` and `uv` on `PATH`;
- a configured provider key in `config.local.toml`.

Nothing here is hardcoded to a particular host: install locations come from
discovery and environment variables.

## Artifacts

Everything lands under `.local/` in this repository (gitignored):

- the fixture modlet `.local/vision-e2e-mod` — created by the script,
  never committed;
- one timestamped run dir per execution, `.local/e2e/<stamp>/`, holding the
  muxed clip, the client log, the raw review stdout, and `evidence.json`.

Re-running the script creates a new stamp dir; an existing evidence envelope
is never overwritten. `--fresh` rebuilds the fixture modlet from scratch.

## Options

| Flag | Meaning |
|---|---|
| `--provider NAME` | provider to review with (default: the configured `default_provider`, else the first configured provider) |
| `--model ID` | pass through to `deadeye review --model` |
| `--game-srv DIR` | the stock dedicated server install |
| `--clip PATH` | skip the in-game capture; review an existing clip (file or frame dir) |
| `--intent FILE` | intent file for the review (default: the fixture's `thing.review.json`) |
| `--fresh` | rebuild the fixture modlet even if it exists |
| `--help` | usage text |

## Review-only mode

To review an already-captured clip (for example, one from an earlier run or
from a `7dtd-playtest` capture) without booting the game:

```bash
scripts/e2e.sh --clip .local/e2e/<stamp>/capture/motion_thing.mp4
```

`--clip` with `--provider fake` runs the whole pipeline offline — no game,
no network, no billable call — which is the cheapest way to prove the
plumbing:

```bash
scripts/e2e.sh --provider fake --clip .local/e2e/<stamp>/capture/motion_thing.mp4
```

## The fixture

The fixture is a synthesized box on a turntable (`shamway generate mesh`,
motion kind `turntable`), deployed as the `VisionE2E` modlet. Its intent
file (`thing.review.json`) is written by the script with the generated
suite and the `motion_thing` case id, so the review always matches the run
that produced the clip.
