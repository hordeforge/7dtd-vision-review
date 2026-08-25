# Integration contract

The sibling repositories call deadeye over a subprocess boundary: `deadeye
review --json` prints the evidence envelope to stdout, and the exit code is
the success signal. This keeps each consuming repository an independent git
tree with no cross-repo Python dependency — the same way they already shell
out to `ffmpeg`, `montage`, and the game client.

## The call

```bash
deadeye review <clip-dir> --intent <intent.json> --provider <name> \
    --model <id> --allow-network --json
```

- exit `0` — stdout is the full evidence envelope (JSON).
- exit non-zero — stderr carries one `ERROR: ...` line; no envelope was
  produced, and no partial verdict may be treated as a completed review.

`--allow-network` is required; a consumer surfaces it as its own consent flag
(`shamway review-video`'s `--allow-network`, for example). Disclosure lines
are written to stderr and are for humans; the caller should surface the
provider, file count, and total bytes before invoking deadeye or relay the
stderr lines after.

## What the consumer adds

The envelope is the model-I/O record. The consumer's own evidence document
embeds it and adds the fields only the consumer knows:

- `7dtd-asset-pipeline` (`shamway review-video`): the reviewed asset's
  generation parameters (mesh seed/shape/size for a synthesized bundle, the
  source file's SHA-256 for an adopted/external one, or an honest "not
  recorded"), plus its own tool version and parameters.
- `7dtd-playtest` (`scripts/review_video.py`): the suite and case the clip
  came from (also carried in the intent), plus its own report wiring.

Both consumers validate the envelope's `result` against the shared schema
before writing evidence, mirroring the audio-review pipeline's rule that a
silently coerced field would put words into the reviewer's mouth.

## Capability probing

A consumer's capability registry probes `deadeye` on PATH (and, for a real
provider, the provider's credential environment) without running a review.
`deadeye doctor --json` reports provider state from environment presence only
and never contacts a provider, so the consumer can show `unavailable`,
`configured`, or `not probed` exactly as the audio-review capability does.

## Versioning

The intent schema, result schema, rubric, and prompt are versioned in the
envelope (`schema_version`, `rubric_version`, `prompt_version`). A consumer
that pins a deadeye release keeps its evidence comparable across revisions;
the envelope's `tool_version` records which release produced it.
