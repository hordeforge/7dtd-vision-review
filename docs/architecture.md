# Architecture

deadeye is deliberately narrow: it owns the provider boundary for vision-model
review and nothing else. The consumers (`7dtd-asset-pipeline`, `7dtd-playtest`)
own the operations, the intent files, the evidence documents that carry fields
only they know (generation parameters, suite and case), and the gates that
decide what a review may and may not do.

## The flow

```
caller ──(deadeye review CLI)──> consent gate → intent validation
       → clip discovery → provider limits → sampling → disclosure
       → provider.review(request) → validate_result → evidence envelope
```

Order matters and is tested: consent first of all (before credentials are
read), then local validation, then limits, then disclosure, then submission,
then structural validation, then evidence. A failure at any step raises one
user-actionable message and preserves no partial verdict as a completed
review.

## Boundaries that must not blur

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

**Credentials never travel or land.** They come only from provider
configuration or environment variables, never as a command argument, and never
in stdout, JSON output, logs, or evidence. The redaction backstop in
`intent.py` drops credential-named keys wherever they would otherwise land.

**Advisory only.** `ADVISORY_NOTE` rides every result and every evidence
envelope: a model critique cannot mark an asset accepted. Human sign-off in
the real context decides that, in the consuming repository's gates.

**Traceable, never deterministic.** Every envelope names the exact bytes
submitted (SHA-256), the sampling that chose them, the rubric and prompt
versions, and the provider. Two runs may disagree; disagreement is preserved,
never averaged. A later review never overwrites an earlier evidence envelope by
default.

## Sampling honesty

Providers differ in what they can ingest. The adapter declares its limits
(`ProviderLimits`); the sampling layer asks before submitting and records what
it did. A muxed video goes inline when the provider takes video and the file
fits; otherwise the frame sequence is sampled down with even spacing, always
keeping the first and last frame. The evidence's `sampling` block names
exactly which files went and what was dropped, so a review that saw only eight
of forty frames says so. A provider that cannot ingest actual media at all is
refused as an adapter, not worked around.
