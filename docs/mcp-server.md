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
  - a key reused with *different* arguments is refused, so one operation's
    verdict is never returned for another's request and a name is never
    spent twice;
  - only completed reviews are recorded, so a local refusal stays retryable
    and an ambiguous timeout is never frozen into a result the client never
    received. A review whose verdict arrived but whose evidence file could
    not be written *was* completed and billed, so it is recorded too, and a
    retry under the same key replays that same fault and envelope instead of
    submitting the media a second time;
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
  like the CLI. An unexpected fault inside one frame answers `-32603` (with the
  trace on stderr) instead of tearing down the session, and a tool call that
  fails outside `DeadeyeError` names the tool and the exception type rather
  than a bare message. A JSON-RPC line larger than 1 MiB is the same parse
  error (`-32700`): the extra bytes are discarded through the next newline so
  the following frame stays aligned, and the process cannot grow with one
  unbounded stdin line.

## Result shapes

Every tool result is one text content part carrying JSON, so a client reads
`result.content[0].text` and parses it:

| Tool | Text payload |
|---|---|
| `review` | the evidence envelope, identical to `deadeye review --json` |
| `doctor` | `{"providers": [...]}`, the entries `deadeye doctor --json` prints as a bare array |
| `schema` | the schema document, identical to `deadeye schema` |
| `prompt` | `{"prompt": "..."}`, the text `deadeye prompt` prints bare |

A refusal is the same text part prefixed `ERROR: ` with `isError: true`. The
one exception is a review whose evidence write failed after a billed
submission: the text part is `{"error": ..., "envelope": ...}` and
`isError` is still true.

`review` accepts `intent` or `intent_text`, never both and never neither: the
core's exactly-one rule applies verbatim, and passing both is the refusal
"takes exactly one of --intent PATH or --intent-text JSON, never both" despite
the JSON-RPC parameter names.

## Out of scope for now

stdio framing and session handling are built; the pinned protocol version is
`2025-06-18`; no third-party MCP SDK is adopted (the surface stays in the
standard library). Deliberately deferred, per the original design: SSE push,
MCP sampling, resources/prompts beyond the tool surface, multi-session
management, and authentication (the server inherits the CLI's env/config
credential boundary). This page exists so the CLI's contract (stderr
disclosure, stdout envelope, exit-code semantics) does not drift into a shape
a server transport could not reuse.
