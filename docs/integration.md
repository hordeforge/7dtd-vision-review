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

## Running a review twice

Deadeye performs exactly one provider submission per invocation and never
retries; duplicate execution comes from callers. A rerun of the same command
is a second billable upload that yields an independent envelope with a fresh
`review_id`: verdicts are not deterministic, and disagreement is preserved,
never averaged. An existing `--output` path is refused without `--force`, so
a rerun cannot silently replace earlier evidence.

If a submission times out or the connection dies before a complete response,
the refusal on stderr states that the attempt may still have completed and
been billed server-side. A caller with its own retry policy must treat that
outcome as ambiguous: resubmitting bills a second review, it does not resume
the first. Retries are therefore safest only after an unambiguous local
refusal (consent, configuration, limits), all of which happen before any
bytes leave the machine.

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
`deadeye doctor --json` reports provider state from local credential presence
(environment or config files) and never contacts a provider, so the consumer
can show `unavailable`, `configured`, or `not probed` exactly as the
audio-review capability does.

## Versioning

The intent schema, result schema, rubric, and prompt are versioned in the
envelope (`schema_version`, `rubric_version`, `prompt_version`). A consumer
that pins a deadeye release keeps its evidence comparable across revisions;
the envelope's `tool_version` records which release produced it.
