# Agent instructions

This repository is **deadeye**, the shared vision-model review gateway for the
hordeforge workspace. Read this before inspecting, planning, editing, or
testing anything here.

## What this repository is

`deadeye` is the one place a hordeforge tool goes to have a clip looked at by
a vision model. It takes a clip (a muxed video or a frame sequence) plus the
author's recorded intent, samples the media down when the provider's budget
demands it, submits it to a configured vision-capable provider, and returns
one stable, pipeline-owned result shape. Consumed programmatically by
`7dtd-asset-pipeline` (`shamway review-video`, via its `video_review.py`,
which invokes the `deadeye` CLI); usable standalone through the `deadeye`
CLI or its MCP server (`deadeye mcp`, [docs/mcp-server.md](docs/mcp-server.md)).

It owns the provider boundary only. It owns no clip, no intent, no game, and
no acceptance decision: a verdict here is advisory evidence, never a sign-off.

## Working on this repository

This repository lives in the **hordeforge** organization
(`github.com/hordeforge/7dtd-vision-review`), alongside the other `7dtd-*`
projects. Work here goes on a branch and lands through a pull request; nothing
is pushed straight to the default branch.

### Boundaries that must not blur

- **Consent comes before everything.** Submitting media is networked,
  billable, and sends authored assets to a third party. Nothing here contacts
  a provider without `--allow-network`, and no refusal reads credentials
  before the consent gate.
- **Credentials never travel or land.** They come from the environment or
  from `config.local.toml` (the gitignored local config; `config.py` owns the
  precedence), never as a command argument, and never in stdout, JSON output,
  logs, or evidence. The redaction backstop in `intent.py` is load-bearing;
  tests pin it.
- **The result schema is ours, not the vendor's.** Provider payloads stay at
  the adapter boundary; callers consume `validate_result`'s output. A raw
  response is preserved only when explicitly requested, redacted either way.
- **Advisory only.** Nothing in this repository can mark an asset accepted;
  human sign-off in the real context decides that. The `ADVISORY_NOTE` rides
  every result and every evidence document.
- **A review is traceable, never deterministic.** Every envelope names the
  exact bytes submitted (SHA-256), the sampling that chose them, the rubric
  and prompt versions, and the provider. Two runs may disagree; disagreement
  is preserved, never averaged. A later review never overwrites an earlier
  evidence envelope by default.

### Fix it upstream, do not work around it here

When work here hits a bug, a missing check, or a confusing default in a
sibling `hordeforge/7dtd-*` repository, fix it there: branch, fix, add the
test that would have caught it, update that repository's own documentation,
push, open a pull request, and merge it autonomously, the same lifecycle this
repository uses. A local workaround for someone else's bug is a second copy of
the problem.

### Documentation is written while the work happens, never afterwards

Every behaviour change updates the documentation in the same commit that makes
it. A new provider goes in `PROVIDERS` (in `surface.py`), the provider index
[docs/providers.md](docs/providers.md), and the provider table in
[docs/reference.md](docs/reference.md); a new command goes in the command
reference [docs/reference.md](docs/reference.md) and the page that owns its
subject; a new adapter goes under `src/deadeye/providers/` with the protocol
already defined in `base.py`. An undocumented capability is one the next
session will rebuild from scratch.

- `scripts/bootstrap` — `uv sync` from the committed lockfile with the dev group
- `make check test` — lint, typecheck, compileall, and the unit suite

```bash
scripts/bootstrap
make check test
```

Use **uv** for every Python step — environments, installs, and runs. Do not
add `pip`, `pipx`, `venv`, or `python -m pip` invocations to scripts, docs, or
CI.

`make check test` must pass before you hand work back. It needs no network, no
credentials, and no model.

## Cost and blast radius

The only expensive step is a real provider review, and it never happens
implicitly: `--allow-network` gates it, the disclosure lines say exactly what
will leave the machine, and the default flow a caller learns first is the
offline fake provider. Do not start a real review speculatively and never in a
loop — every submission is billable and sends authored media to a third party.

## Provider additions

Adding a provider means:

1. a module under `src/deadeye/providers/` implementing the protocol in
   `base.py` (limits, credential presence, review) with the standard library;
2. one line in `PROVIDERS` in `surface.py`;
3. a row in the provider table in [docs/reference.md](docs/reference.md) and
   a section in [docs/providers.md](docs/providers.md);
4. a fake-boundary test that proves actual media bytes reach the adapter
   (`providers/fake.py` shows the pattern), plus an opt-in live test that
   never runs in the offline suite.

The capability registry (`deadeye doctor`) must report state from environment
presence only and must never contact a provider during discovery.
