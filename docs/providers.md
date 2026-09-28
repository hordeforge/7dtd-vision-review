# Providers

An adapter is a narrow protocol in `src/deadeye/providers/base.py`:

- `limits` — accepted suffixes, per-request byte budget, maximum frames, and
  whether a muxed video can be submitted as-is;
- `is_configured()` / `configuration_hint()` — local credential presence
  (environment or `config.local.toml`) only, so `deadeye doctor`, `--help`,
  and offline runs never contact a provider; the hosted adapters inherit the
  first from `CredentialedProvider` in `base.py` and write only the second;
- `review(request)` — submit media plus prompt, return raw text plus usage
  metadata, raising `DeadeyeError` on refusal or fault.

A `ReviewRequest` carries the two halves of the instruction separately:
`system_prompt` is the pipeline-owned reviewer instruction, `prompt` is the
authored intent, and `rendered` is both as one string (what the evidence
envelope records). An adapter whose endpoint has a system role sends the
instruction there, so intent text cannot occupy the slot the output contract
and rubric sit in.

Adapters speak HTTP with the standard library. A build tool that already
carries no SDK has no reason to grow one, and every dependency avoided is a
supply-chain surface a consuming mod author never has to audit. Generation
parameters are read through the shared validated readers in `providers/base.py`: an
absent key falls back to the adapter's built-in default, while a value that
is present but unusable (a string where a number belongs, a boolean, a
non-finite float) is refused with the key named before any submission — a
silently substituted parameter would make the evidence untraceable to its
configuration. The output caps (`max_output_tokens`, `max_tokens`) additionally
require a value of at least 1: a provider that reads zero or a negative cap as
"no limit" would turn a botched key into an unbounded billable generation,
which is the one outcome those knobs exist to prevent.

The shared HTTP layer decodes the response envelope explicitly: the charset
declared in `Content-Type` when it decodes, UTF-8 (JSON's default) otherwise,
and a body that decodes as neither is refused with one error naming the
provider — the same fault family as any other malformed answer — rather than
escaping as a raw decode crash after a billed submission or manufacturing
replacement characters into evidence. Successful JSON bodies are capped at
8 MiB; a 4xx/5xx body is read only up to the 300-character fault slice, then
the socket is closed, so a hostile or misconfigured endpoint cannot retain
an unbounded error payload in the long-lived MCP server.

## fake

`fake` is the offline stand-in. It answers from the request envelope alone and
sees nothing, which is the point: the tests assert on what it *received* (the
exact media bytes, by hash, and the complete prompt), pinning the boundary's
contract without any network. It is also the dry-run lane for a caller proving
intent and evidence plumbing before paying for a real submission.

## gemini

`gemini` is the first hosted adapter. Google's Gemini API accepts video and
multi-image input inline (base64, no upload round trip) and can be asked for
JSON output. A muxed video goes inline when it fits the ~20 MB per-request
budget; otherwise the sampled frame sequence goes as multi-image input — the
broadly supported fallback every vision-chat API shares. Byte budgets count
what the wire carries: every adapter submits inline base64, so 3 raw bytes
are charged as 4, and the local budget checks compare the encoded size. The
budget bounds the whole request, so reference media and the reviewer prompt
are counted against it too, and a muxed video that fits alone but not beside
them falls back to the frame sequence instead of overrunning the request.

The key arrives from `GEMINI_API_KEY` or `GOOGLE_API_KEY`, travels in a header
(never a query string), and is never printed, logged, or written into
evidence. The default model is a default, not a contract: pass `--model` to
override.

Generation always carries a `maxOutputTokens` cap (module constant
`DEFAULT_MAX_OUTPUT_TOKENS`, the model's published ceiling) so a looping or
runaway generation cannot bill without end; override it per setup with
`providers.gemini.max_output_tokens`. The cap exists to stop runaway spend,
not to shape answers, so it sits at the ceiling rather than a tight budget.
The request also states `temperature` (module constant
`DEFAULT_TEMPERATURE`) rather than leaving it to the provider's default: the
2.5 series defaults to `1.0`, and a review is meant to be traceable to the
submission that produced it, not drawn from a distribution a server-side
default can widen without a version bump. Override it with
`providers.gemini.temperature`. The reviewer instruction rides
`systemInstruction` and the authored intent the `user` turn, which is where
Gemini gives an instruction its standing.

The verdict shape rides `generationConfig.responseSchema` beside the JSON
mime type, so the decoder is constrained to the keys the reviewer instruction
names: every required result key, and every rubric dimension spelled out from
`BASE_RUBRIC`. A model that would otherwise answer in prose, or with a key the
instruction never mentioned, cannot, so a schema mismatch does not turn a
billed submission into a post-hoc refusal. The schema accepts a moment as a
single number rather than a `[start, end]` pair, since the OpenAPI subset
Gemini takes has no union type; that is the narrower of the two forms
`validate_result` already accepts.

The live path is covered by an opt-in test
(`DEADEYE_NETWORK_TESTS=gemini` + `GEMINI_API_KEY`); the offline suite pins
limits, MIME mapping, credential presence, and the request body shape instead.

## nvidia

`nvidia` is the second hosted adapter: NVIDIA's NIM chat-completions endpoint
(`integrate.api.nvidia.com/v1/chat/completions`), an OpenAI-compatible
vision-chat surface. A muxed video goes as a single `video_url` content part
(NVIDIA's documented form for video: "Videos use type = video_url"); local
frames go as base64 data URLs in `image_url` parts — no upload round trip.
The omni default model is video-capable, so the sampling layer prefers the
muxed video and falls back to the frame sequence (sampled down to the
adapter's 12-image budget, recorded in the evidence) only when no video fits.
The default model is `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` with
the generation settings its verified payload uses (`max_tokens`,
`reasoning_budget`, `temperature`, `top_p`), each a module constant a
deployment may override under `[providers.nvidia]`.

The key arrives from `NVIDIA_API_KEY`, travels in an `Authorization` header
(never a query string), and is never printed, logged, or written into
evidence. The reviewer instruction is its own `system` message ahead of the
`user` message carrying the authored intent and the media.

The live path is covered by an opt-in test
(`DEADEYE_NETWORK_TESTS=nvidia` + `NVIDIA_API_KEY`); the offline suite pins
limits, MIME mapping, credential presence, and the exact request body —
including that frames travel as base64 bytes, never filesystem paths.

## Adding one

1. a module under `src/deadeye/providers/` implementing the protocol in
   `base.py` with the standard library; a hosted provider that takes a key
   subclasses `CredentialedProvider` for the credential, model, and limits
   every adapter shares, and implements `configuration_hint()` and
   `review()`;
2. one line in `PROVIDERS` in `surface.py`;
3. a row in the provider table in [reference.md](reference.md) and a section
   on this page;
4. an offline test proving actual media bytes reach the adapter (`fake.py`'s
   boundary test shows the pattern), plus an opt-in live test that never runs
   in the offline suite.

A provider that cannot ingest actual media (a stills-only or transcription-only
endpoint) does not meet this capability and is refused as an adapter.
