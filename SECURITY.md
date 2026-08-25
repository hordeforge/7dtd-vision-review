# Security Policy

## Supported versions

Only the latest release is supported: **0.1.0** (canonical constant in
`src/deadeye/_version.py`, mirrored by `pyproject.toml`; older releases
receive no fixes).

## Trust boundaries

- **Credentials never travel through the command line.** An API key comes
  from the environment or from `config.local.toml`, never from a flag, and is
  never printed, logged, or written into an evidence envelope. `deadeye
  doctor` reports which file a key came from and never its value. Tool and
  parameter information stored in evidence has credentials removed
  (`src/deadeye/evidence.py`).
- **`config.local.toml` is gitignored and must stay that way.** It is the only
  file in the tree expected to hold a secret. Committing one leaks a
  provider key.
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
