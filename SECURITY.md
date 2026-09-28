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
  is a gate.
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
