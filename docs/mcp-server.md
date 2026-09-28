# MCP server

Implemented (2026-08-25): `deadeye mcp` serves the CLI surface as a Model
Context Protocol server on stdio — newline-delimited JSON-RPC 2.0, no third-
party SDK — so the same review capability is reachable from any MCP client
(an agent, a dashboard, a homegrown control script) without a subprocess.
`tests/test_mcp.py` pins the protocol offline: handshake, tool listing, tool
calls, spec error codes, and the review consent boundary.

## Design intent

- **Same contract, different transport.** The MCP tools map onto the CLI's
  surface: a `review` tool (clip directory or video, intent, provider, model)
  returning the same evidence envelope, a `doctor` tool reporting capability
  state, a `schema` tool describing the intent/result shapes, and a `prompt`
  tool rendering the injected reviewer instruction. No new authority model,
  no second result format.
- **Consent and credentials do not weaken.** `--allow-network` becomes an
  explicit per-call JSON boolean that refuses the upload unless it is exactly
  `true`; the optional `force` and `keep_raw_response` controls are likewise
  JSON booleans, never truthy strings. Credentials still come from the
  environment or loaded configuration (normally the gitignored
  `config.local.toml`); disclosure lines still precede submission.
- **Duplicate calls are duplicate submissions unless the client names the
  operation.** A client that resends a `review` call (lost response,
  timeout, replay) with no `idempotency_key` triggers a second billable
  submission rather than retrieving the first attempt's verdict; the tool
  description says so, and ambiguous transport failures carry the same
  warning as the CLI's. A client that *does* pass an `idempotency_key` gets
  the guarantee it asked for: a repeated call with the same key and the same
  arguments returns the first attempt's envelope verbatim, submitting
  nothing, for as long as the server process lives. The key must be a
  non-empty string of at most 200 characters, and the ledger holds the most
  recent 128 completed keys (least recently used evicted) and at most 32 MiB
  of retained envelopes, whichever bound the next entry crosses first, so it
  cannot grow without bound in a long-lived server. The byte budget matters
  because an entry's size is the client's to choose: one carrying
  `keep_raw_response` holds a redacted provider payload, which the HTTP
  reader bounds at 8 MiB. The newest entry is always kept whatever it
  weighs, so a key the client is about to retry still replays instead of
  billing twice. Three properties make the replay honest rather than
  convenient:
  - a key is compared in Unicode NFC, so the same name reaches the ledger as
    one entry whether the client composed it or received it decomposed (macOS
    composes nothing it receives). The 200-character cap is applied after the
    fold, so a decomposed key cannot shrink its way past it;
  - a key reused with *different* arguments is refused, so one operation's
    verdict is never returned for another's request and a name is never
    spent twice;
  - only submissions that reached the provider are recorded, so a refusal
    raised before anything was sent stays retryable while a call that was
    billed is never offered back as a safe retry. A review whose verdict
    arrived but whose evidence file could not be written replays that same
    fault and envelope; a review the provider answered with nothing usable
    (a timeout, an answer the adapter cannot use, an envelope no verdict
    can be read out of, a response that failed
    result validation) replays that same refusal. A status the provider
    refused before running the review (a rejected credential, a quota, a bad
    request) spent nothing, so that key stays free for a corrected retry.
    Neither submits the media a second time, and a client that
    genuinely wants another attempt names a new key;
  - the ledger is process-local, so the guarantee covers replay within one
    session, not a restart; across restarts the client is back to the
    default of a duplicate call being a new submission.
- **A partial failure does not force a resend.** When a review completes but
  its evidence file cannot be written, the `isError` tool result carries the
  full envelope beside the error text, so the client recovers the billed
  verdict without submitting the media again.
- **stdout stays clean.** The MCP server speaks JSON-RPC on stdio (the
  standard MCP transport), which is why the CLI already routes disclosure to
  stderr: a future `deadeye serve` replaces the argparse dispatcher, not the
  core.
- **Fail closed.** Malformed frames get spec JSON-RPC errors; unknown tools
  error; a review that would need the network refuses without consent, exactly
  like the CLI. Every argument is read and typed at the boundary, before
  anything is submitted: `allow_network`, `force`, and `keep_raw_response` must
  be literal JSON booleans, `clip`, `intent`, `intent_text`, `output`, and
  `model` must be strings, and `provider` must name a registered provider (the
  same list `--provider` draws from, also published as the tool's `enum`). A
  JSON `null` reads as an absent argument, the same as leaving the key out, so
  the optional routes behave one way. Each refusal names the tool and the
  argument, which is what `--provider` does through argparse and what the
  timeout already did; an argument of the wrong type is no longer a fault
  report about `TypeError` or `KeyError`. An unexpected fault inside one frame
  answers `-32603` (with the trace on stderr) instead of tearing down the
  session, and a tool call that fails outside `DeadeyeError` names the tool
  and the exception type rather than a bare message. A JSON-RPC line larger
  than 1 MiB is the same parse error (`-32700`): the extra bytes are discarded
  through the next newline so the following frame stays aligned, and the
  process cannot grow with one unbounded stdin line. The only line the loop
  passes without an answer is one holding nothing but JSON whitespace (space,
  tab, CR, LF). Any other unparsable line is answered `-32700`, including one
  made of characters Python calls whitespace and JSON does not (U+001C,
  U+0085, U+2028), so a client waiting on a frame it wrote is never left on
  silence.

## Result shapes

Every tool result is one text content part carrying JSON, so a client reads
`result.content[0].text` and parses it:

| Tool | Text payload |
|---|---|
| `review` | the evidence envelope, identical to `deadeye review --json` |
| `doctor` | `{"providers": [...], "config": {...}}`, the entries `deadeye doctor --json` prints as a bare array plus the effective-configuration diagnosis it prints beside it |
| `schema` | the schema document, identical to `deadeye schema` |
| `prompt` | `{"prompt": "..."}`, the text `deadeye prompt` prints bare |

A refusal is the same text part prefixed `ERROR: ` with `isError: true`. The
one exception is a review whose evidence write failed after a billed
submission: the text part is `{"error": ..., "envelope": ...}` and
`isError` is still true.

`review` accepts `intent` or `intent_text`, never both and never neither: the
core's exactly-one rule applies verbatim, and passing both is the refusal
"takes exactly one of --intent PATH or --intent-text JSON, never both" despite
the JSON-RPC parameter names. The published input schema states the rule as a
`oneOf` over the two parameters, so a client building a call from `tools/list`
reads it where it reads every other argument instead of discovering it from a
refusal.

`params` and `tools/call`'s `arguments` are objects: present but not an object
is `-32602`, including the falsy forms (`[]`, `""`, `0`, `false`). Omitted or
explicitly null is absent, and is served.

A tool takes exactly the arguments its schema publishes
(`additionalProperties: false`), and an undeclared one is refused by name
before anything is submitted. A misspelled `intetnt` is otherwise dropped in
silence, and the client then collects a refusal about the intent route it
believes it supplied.

## A session

Frames in, one response per line out, newline-delimited. This is a real
transcript against the offline `fake` provider, with the review envelope
trimmed to its keys:

```
$ deadeye mcp
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"deadeye","version":"0.1.1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"review","arguments":{"clip":"clip/","intent":"intent.json","provider":"fake","allow_network":true}}}
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\n  \"kind\": \"deadeye-review\",\n ... }"}]}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"review","arguments":{"clip":"clip/","provider":"genimi","allow_network":true}}}
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"ERROR: review parameter 'provider' 'genimi' is not one of fake, gemini, nvidia"}],"isError":true}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"review","arguments":{"clip":7,"allow_network":true}}}
{"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"ERROR: review parameter 'clip' must be a string"}],"isError":true}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

A notification (no `id` member) gets no response, as the spec requires.

## Out of scope for now

stdio framing and session handling are built; the pinned protocol version is
`2025-06-18`; no third-party MCP SDK is adopted (the surface stays in the
standard library). Deliberately deferred, per the original design: SSE push,
MCP sampling, resources/prompts beyond the tool surface, multi-session
management, and authentication (the server inherits the CLI's env/config
credential boundary). This page exists so the CLI's contract (stderr
disclosure, stdout envelope, exit-code semantics) does not drift into a shape
a server transport could not reuse.
