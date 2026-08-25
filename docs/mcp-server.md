# MCP server (planned)

The CLI is the transport today; a Model Context Protocol server is the
planned second surface, so the same review capability becomes reachable from
any MCP client (an agent, a dashboard, a homegrown control script) over
standard JSON-RPC instead of a subprocess.

## Design intent

- **Same contract, different transport.** The MCP tools map onto the CLI's
  surface: a `review` tool (clip directory or video, intent, provider, model)
  returning the same evidence envelope, a `doctor` tool reporting capability
  state, and a `schema` tool describing the intent/result shapes. No new
  authority model, no second result format.
- **Consent and credentials do not weaken.** `--allow-network` becomes an
  explicit per-call parameter that refuses the upload when unset; credentials
  still come only from the environment; disclosure lines still precede
  submission.
- **stdout stays clean.** The MCP server speaks JSON-RPC on stdio (the
  standard MCP transport), which is why the CLI already routes disclosure to
  stderr: a future `deadeye serve` replaces the argparse dispatcher, not the
  core.
- **Fail closed.** Malformed frames get spec JSON-RPC errors; unknown tools
  error; a review that would need the network refuses without consent, exactly
  like the CLI.

## Out of scope for now

stdio framing and session handling are only designed, not built; the MCP spec
version is not pinned; no third-party MCP SDK is adopted yet. This page exists
so the CLI's contract (stderr disclosure, stdout envelope, exit-code
semantics) does not drift into a shape a server transport could not reuse.
Build the server only when a real consumer needs an MCP client to reach it.
