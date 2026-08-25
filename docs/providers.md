# Providers

An adapter is a narrow protocol in `src/deadeye/providers/base.py`:

- `limits` — accepted suffixes, per-request byte budget, maximum frames, and
  whether a muxed video can be submitted as-is;
- `is_configured()` / `configuration_hint()` — environment presence only, so
  `deadeye doctor`, `--help`, and offline runs never contact a provider;
- `review(request)` — submit media plus prompt, return raw text plus usage
  metadata, raising `DeadeyeError` on refusal or fault.

Adapters speak HTTP with the standard library. A build tool that already
carries no SDK has no reason to grow one, and every dependency avoided is a
supply-chain surface a consuming mod author never has to audit.

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
broadly supported fallback every vision-chat API shares.

The key arrives from `GEMINI_API_KEY` or `GOOGLE_API_KEY`, travels in a header
(never a query string), and is never printed, logged, or written into
evidence. The default model is a default, not a contract: pass `--model` to
override.

The live path is covered by an opt-in test
(`DEADEYE_NETWORK_TESTS=gemini` + `GEMINI_API_KEY`); the offline suite pins
limits, MIME mapping, credential presence, and the request-body labels instead.

## Adding one

1. a module under `src/deadeye/providers/` implementing the protocol in
   `base.py` with the standard library;
2. one line in `PROVIDERS` in `cli.py`;
3. a row in the README provider table and a section on this page;
4. an offline test proving actual media bytes reach the adapter (`fake.py`'s
   boundary test shows the pattern), plus an opt-in live test that never runs
   in the offline suite.

A provider that cannot ingest actual media (a stills-only or transcription-only
endpoint) does not meet this capability and is refused as an adapter.
