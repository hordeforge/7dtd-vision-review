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
- **Duplicate calls are duplicate submissions.** The server keeps no state
  between frames, so a client that resends a `review` call (lost response,
  timeout, replay) triggers a second billable submission rather than
  retrieving the first attempt's verdict; the tool description says so, and
  ambiguous transport failures carry the same warning as the CLI's. One
  partial failure is not allowed to force that resend: when a review
  completes but its evidence file cannot be written, the `isError` tool
  result carries the full envelope beside the error text, so the client
  recovers the billed verdict without submitting the media again.
- **stdout stays clean.** The MCP server speaks JSON-RPC on stdio (the
  standard MCP transport), which is why the CLI already routes disclosure to
  stderr: a future `deadeye serve` replaces the argparse dispatcher, not the
  core.
- **Fail closed.** Malformed frames get spec JSON-RPC errors; unknown tools
  error; a review that would need the network refuses without consent, exactly
  like the CLI. An unexpected fault inside one frame answers `-32603` (with the
  trace on stderr) instead of tearing down the session, and a tool call that
  fails outside `DeadeyeError` names the tool and the exception type rather
  than a bare message.

## Out of scope for now

stdio framing and session handling are built; the pinned protocol version is
`2025-06-18`; no third-party MCP SDK is adopted (the surface stays in the
standard library). Deliberately deferred, per the original design: SSE push,
MCP sampling, resources/prompts beyond the tool surface, multi-session
management, and authentication (the server inherits the CLI's env/config
credential boundary). This page exists so the CLI's contract (stderr
disclosure, stdout envelope, exit-code semantics) does not drift into a shape
a server transport could not reuse.
