# Architecture

deadeye is deliberately narrow: it owns the provider boundary for vision-model
review and nothing else. The consumers (`7dtd-asset-pipeline`, `7dtd-playtest`)
own the operations, the intent files, the evidence documents that carry fields
only they know (generation parameters, suite and case), and the gates that
decide what a review may and may not do.

## The flow

```
caller ──(deadeye review CLI)──> consent gate → evidence-path guard
       → intent validation → clip discovery → provider limits → sampling
       → disclosure → provider.review(request) → validate_result
       → evidence envelope
```

Order matters and is tested: consent first of all (before credentials are
read), then the evidence-path guard (a rerun into an occupied `--output` is
refused before anything is contacted), then local validation, then limits,
then disclosure, then submission, then structural validation, then evidence. A
failure at any step raises one user-actionable message and preserves no
partial verdict as a completed review.

## Boundaries that must not blur

**The prompt is built for you, not by you.** The gateway assembles the
reviewer instruction from the intent (`purpose`, `subject`, `camera_path`,
`desired_qualities`, `avoid`, `questions`) plus the versioned rubric and the
exact JSON result shape, and announces what media actually reached the model.
The instruction and the intent travel in separate roles: the instruction is
the provider's system instruction, and the intent is the only thing in the
user turn, fenced between BEGIN/END markers the instruction has already
declared as data. An intent may steer what the model looks at, never how it
answers, and it cannot occupy the slot the contract and rubric sit in.
Intent text or a reference filename carrying a fence marker of its own is
refused locally, so the fence cannot be closed early and spoken around, and
filenames rendered into prompt text have control characters flattened so no
line can be forged inside them. A caller never writes or passes a prompt;
`deadeye prompt` renders the assembled instruction for inspection before
submission. The prompt and rubric versions ride in the evidence so a
review is traceable to the instruction it answered.

**Consent comes before everything.** Submitting media is networked, billable,
and sends authored assets to a third party. Nothing contacts a provider
without `--allow-network`, and no refusal reads credentials before the consent
gate. The tests pin this by making `is_configured` unreachable before consent.

**The result schema is ours, not the vendor's.** Provider payloads stay at the
adapter boundary; callers consume `validate_result`'s output. A raw response
is preserved only when explicitly requested, redacted either way. The shape is
the same family the audio-review pipeline uses (`summary`, `strengths`,
`issues`, `recommended_changes`, `rubric_scores`, `confidence`,
`limitations`), so a caller handling both review kinds reads one shape.

**Credentials never travel or land.** They come from the environment or from
`config.local.toml` (the gitignored local config; see `config.py` for the
precedence, which is per setting: command-line review options override their
configured counterparts, credentials prefer the environment over the merged
configuration, and every other setting comes from the merged configuration
with a built-in default behind it), never as a command argument, and never in
stdout, JSON output, logs, or evidence. The redaction backstop in `intent.py`
and `redaction.py` drops credential-named keys wherever they would otherwise
land, and bounds its own walk depth so a deeply nested payload is dropped
rather than escaping as an uncaught error from a submission that was already
billed.

**Text is not ASCII by construction.** A model's prose, an author's intent, and
a filename are all non-ASCII by nature, and every encoding decision in the
gateway is explicit for that reason: file reads are `rb` plus a named decode
(including `utf-8-sig`, so an editor's BOM is not a parse error), provider
bodies take the declared charset and fall back to UTF-8, evidence is written
as bytes (`_atomic_write`) so the stored SHA-256 matches the file and the
platform's newline translation cannot rewrite it, and `post_json` sends
`ensure_ascii` JSON. Filenames and the intent reach the prompt through
`flat_label_text`, which replaces every non-printable character (including
category Cf: bidi controls, zero-width joiners) with a space so a name cannot
forge a label-shaped line. Both presentation streams are bound to UTF-8 with
`backslashreplace` in `_streams.py`: under a C or POSIX locale Python would
otherwise bind stdout to ASCII and raise `UnicodeEncodeError` inside `print`
after a billable submission, losing the verdict the caller paid for.

**Advisory only.** `ADVISORY_NOTE` rides every result and every evidence
envelope: a model critique cannot mark an asset accepted. Human sign-off in
the real context decides that, in the consuming repository's gates.

**Traceable, never deterministic.** Every envelope names the exact bytes
submitted (SHA-256), the sampling that chose them, the rubric and prompt
versions, and the provider. `created_utc` is the envelope's UTC instant
(RFC 3339 with an explicit offset); `elapsed_seconds` is the monotonic
duration of the provider call, so an NTP step is not recorded as model
latency. Two runs may disagree; disagreement is preserved, never averaged. A
later review never overwrites an earlier evidence envelope by default. The
write occupies the destination name with `O_CREAT|O_EXCL` before the atomic
replace, so two concurrent reviews of the same `--output` path cannot both
publish: the first envelope stays, the second is refused.

## Sampling honesty

Providers differ in what they can ingest. The adapter declares its limits
(`ProviderLimits`); the sampling layer asks before submitting and records what
it did. A muxed video goes inline when the provider takes video and the file
fits; otherwise the frame sequence is sampled down with even spacing, always
keeping the first and last frame. The budget that decides is the whole
request's, prompt included, so a video that clears the provider's video
budget and still will not fit beside the prompt is replaced by the frame
sequence rather than refused outright. The evidence's `sampling` block names
exactly which files went and what was dropped, so a review that saw only eight
of forty frames says so. A provider that cannot ingest actual media at all is
refused as an adapter, not worked around.
