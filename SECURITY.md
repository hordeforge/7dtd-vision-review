# Security Policy

## Supported versions

Only the latest tagged release is supported; older releases receive no
fixes, and no release is patched in place: a fix ships in a new tag. The
version lives in `src/deadeye/_version.py`, mirrored by `pyproject.toml`, and
the release is the `vX.Y.Z` tag the artifacts are built from, so the release
notes that state which version carries a fix are the changelog section for
that tag.

## Trust boundaries

- **Credentials never travel through the command line.** An API key comes
  from the environment or from `config.local.toml`, never from a flag, and is
  never printed, logged, or written into an evidence envelope. `deadeye
  doctor` reports where a key came from — the environment, or which config
  file holds it — and never its value. Tool and
  parameter information stored in evidence has credentials removed
  (`src/deadeye/evidence.py`).
- **`config.local.toml` is gitignored and must stay that way.** It is the only
  file in the tree expected to hold a secret. Committing one leaks a
  provider key.
- **A config file in the current directory wins.** Discovery prefers
  `DEADEYE_CONFIG_DIR`, then the working directory, then the user's config
  directory (`src/deadeye/config.py`), so a checkout's own `config.toml`
  shadows your home configuration. Run a review from a directory you trust,
  and treat a per-provider `endpoint` override as a credential-egress
  decision: it is where the authenticated request, the key, and all the media
  go. The disclosure lines name the provider but not the host. The same file
  also chooses `default_model`, the per-provider `model`, and the generation
  knobs (`temperature`, `max_output_tokens`, `top_p`, `max_tokens`,
  `reasoning_budget`; `src/deadeye/config.py:77-94`), so a shadowing config
  changes which model a verdict is attributed to and how it samples, not just
  where the bytes go. The resolved model and generation parameters are recorded
  in the evidence envelope (`src/deadeye/evidence.py:133`).
- **Uploading media requires explicit consent.** No real provider is contacted
  without `--allow-network`; the `fake` provider is offline by construction.
  Sampled frames, muxed clips, and intent-declared reference media leave the
  machine when the flag is given, so treat everything named in the stderr
  disclosure lines as published. A `client.log` sitting beside the frames is
  discovered but never submitted or stored (`src/deadeye/sampling.py`
  discovers it; nothing reads it), so log contents stay local today — do not
  rely on that staying true without re-checking.
- **A provider response is untrusted input.** It is parsed and validated into
  the result shape before use (`src/deadeye/result.py`); a malformed or
  hostile response is a refusal, never a partially applied verdict.
- **The author's statement is data, not instruction.** Intent text and
  reference filenames reach the model inside a declared data-only fence
  (`src/deadeye/prompt.py`), and a field carrying a fence marker is refused
  (`src/deadeye/intent.py`). That bounds the textual escape only: a clip
  carrying rendered instructions is pixels the model reads, so a hostile
  intent or hostile frame can still shape the critique. Nothing in the result
  is a gate. The same content travels back to whoever reads the evidence
  envelope, which carries the intent text and the rendered prompt verbatim, so
  a reviewer reading a stored review is reading authored text with the
  standing of recorded evidence.
- **The evidence destination is the caller's choice, and `--force` is a real
  overwrite.** `--output` / the MCP `output` argument is used as given: the
  parent directories are created on demand and nothing confines the path to
  the clip's tree (`src/deadeye/evidence.py`). Without `--force` an existing
  envelope is never replaced; with it, the exclusive publish is skipped and
  the file at that path is replaced, leaving no record of what was there.
  Point `--output` somewhere only you can write.
- **An MCP client holds the process's authority.** The `review` tool takes the
  clip, the intent, the destination path, the overwrite flag, and the upload
  consent as arguments of one call, and there is no second identity or path
  confinement behind them (`src/deadeye/mcp.py`). It also takes an
  `idempotency_key`, which pins a full result envelope in the server's
  process-local ledger until it is evicted, so a client chooses what stays
  resident in memory. The other tools read, and write nothing: `doctor` and
  `schema` read config and schemas, and `prompt` takes a client-named `clip`
  path and describes whatever media discovery finds there, with no consent gate
  because nothing is submitted (`src/deadeye/mcp.py:214-252`). Treat the stdio
  server as granting whatever its client already has, and do not point an
  automatically-driven client at paths you would not delete by hand.
- **A verdict is advisory, never an acceptance.** `ADVISORY_NOTE` rides every
  result. A consuming repository that gates on a deadeye verdict alone has
  moved a human sign-off into a model, which is a security decision, not a
  convenience.

The full attack surface — entry points, trust boundaries, ranked threats, and
abuse cases with file references — lives in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Reporting

No private disclosure contact or process is defined in this repository. Open
an issue at
<https://github.com/hordeforge/7dtd-vision-review/issues> for anything that
does not itself disclose a live credential.
