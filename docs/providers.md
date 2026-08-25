# Providers

An adapter is a narrow protocol in `src/deadeye/providers/base.py`:

- `limits` — accepted suffixes, per-request byte budget, maximum frames, and
  whether a muxed video can be submitted as-is;
- `is_configured()` / `configuration_hint()` — local credential presence
  (environment or `config.local.toml`) only, so `deadeye doctor`, `--help`,
  and offline runs never contact a provider;
- `review(request)` — submit media plus prompt, return raw text plus usage
  metadata, raising `DeadeyeError` on refusal or fault.

Adapters speak HTTP with the standard library. A build tool that already
carries no SDK has no reason to grow one, and every dependency avoided is a
supply-chain surface a consuming mod author never has to audit. Generation
parameters are read through the shared validated readers in `base.py`: an
absent key falls back to the adapter's built-in default, while a value that
is present but unusable (a string where a number belongs, a boolean, a
non-finite float) is refused with the key named before any submission — a
silently substituted parameter would make the evidence untraceable to its
configuration.

The shared HTTP layer decodes the response envelope explicitly: the charset
declared in `Content-Type` when it decodes, UTF-8 (JSON's default) otherwise,
and a body that decodes as neither is refused with one error naming the
provider — the same fault family as any other malformed answer — rather than
escaping as a raw decode crash after a billed submission or manufacturing
replacement characters into evidence.

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
are charged as 4, and the local budget checks compare the encoded size.

The key arrives from `GEMINI_API_KEY` or `GOOGLE_API_KEY`, travels in a header
(never a query string), and is never printed, logged, or written into
evidence. The default model is a default, not a contract: pass `--model` to
override.

Generation always carries a `maxOutputTokens` cap (module constant
`DEFAULT_MAX_OUTPUT_TOKENS`, the model's published ceiling) so a looping or
runaway generation cannot bill without end; override it per setup with
`providers.gemini.max_output_tokens`. The cap exists to stop runaway spend,
not to shape answers, so it sits at the ceiling rather than a tight budget.

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
`reasoning_budget`, `temperature`, `top_p`), all module constants rather
than per-review knobs.

The key arrives from `NVIDIA_API_KEY`, travels in an `Authorization` header
(never a query string), and is never printed, logged, or written into
evidence.

The live path is covered by an opt-in test
(`DEADEYE_NETWORK_TESTS=nvidia` + `NVIDIA_API_KEY`); the offline suite pins
limits, MIME mapping, credential presence, and the exact request body —
including that frames travel as base64 bytes, never filesystem paths.

## Adding one

1. a module under `src/deadeye/providers/` implementing the protocol in
   `base.py` with the standard library;
2. one line in `PROVIDERS` in `surface.py`;
3. a row in the README provider table and a section on this page;
4. an offline test proving actual media bytes reach the adapter (`fake.py`'s
   boundary test shows the pattern), plus an opt-in live test that never runs
   in the offline suite.

A provider that cannot ingest actual media (a stills-only or transcription-only
endpoint) does not meet this capability and is refused as an adapter.
