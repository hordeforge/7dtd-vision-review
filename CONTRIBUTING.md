# Contributing

Contributions must preserve two boundaries. Everything else is negotiable.

```bash
scripts/bootstrap
make check test
```

Agent-facing rules live in [AGENTS.md](AGENTS.md) and apply to human
contributors too. The organization-wide rules are in
[hordeforge/.github](https://github.com/hordeforge/.github/blob/main/REPOSITORY_STANDARDS.md).

## The two boundaries

**A verdict is advisory, never an acceptance.** `ADVISORY_NOTE` rides every
result and `rubric_scores` are diagnostic, never pass/fail. A change that
lets this repository emit something a consumer could reasonably gate on
without a human has moved a sign-off into a model. Do not make it.

**Nothing reaches a provider without `--allow-network`.** The `fake` provider
is offline by construction, and the entire suite runs with no network and no
credential. A test that reads an API key or opens a socket does not belong
here; add a fixture instead.

## The suite is offline, and that is a gate

`make check test` needs no network, no credential, and no game install. The
fake provider pins the boundary at exact bytes and a complete intent, which
is what makes a provider adapter's behaviour testable at all.

Do not weaken that to make a change pass. If a change genuinely cannot be
tested offline, say so in the pull request and describe what was executed by
hand, with which provider and model. **Evidence is graded**: compiled (it
imports and type-checks), probed (it ran against the fake provider), executed
(it ran against a real provider on real media). Never describe the first as
the third.

## Adding a provider

A provider adapter goes in `src/deadeye/providers/` and implements the base
contract. It must:

- speak HTTP with the standard library. The core carries **no runtime
  dependencies** on purpose: a consuming tool that already ships no SDK has
  no reason to grow one. A provider that needs a vendor SDK is a discussion,
  not a pull request.
- take its credential from the environment or `config.local.toml`, never from
  a command-line flag, and never print, log, or store it.
- declare which media it accepts (frames, muxed video, or both) and its
  limits, so `sampling` can record exactly what went and what was dropped.
- treat the response as untrusted input: parse and validate into the result
  shape, and refuse rather than partially apply a malformed one.
- report its capability state to `deadeye doctor` without contacting the
  provider.
- arrive with tests against a recorded response fixture, covering both an
  accepted response and a rejected one.

Document it in the README provider table and in
[docs/providers.md](docs/providers.md) in the same change.

## Changing a contract

The intent schema, the result shape, and the evidence envelope are consumed
by `7dtd-asset-pipeline` (`shamway review-video`) and `7dtd-playtest`
(`scripts/review_video.py`). Changing any of them is a breaking change for
those repositories:

- bump `schema_version` where the intent file is concerned,
- record it under a **breaking** heading in [CHANGELOG.md](CHANGELOG.md),
- and say in the pull request what the consuming repositories must do.

The call contract is documented in
[docs/integration.md](docs/integration.md); keep it true in the same change,
not afterwards.

## House rules

- Documentation is written while the work happens, never afterwards.
- Fix it upstream rather than working around it here.
- Scratch files go in `.scratch/`, which is gitignored. Never in `src/`,
  never loose in the root.
- Never commit `config.local.toml`, an API key, a machine path, or captured
  media you do not own.
- Commit and pull-request messages must not contain `Co-Authored-By` trailers
  or tool-generated attribution.

## Releases

Bump `version` in [pyproject.toml](pyproject.toml) and its mirror in
`src/deadeye/_version.py`, land that on `main`, then push a matching `vX.Y.Z`
tag. A tag that disagrees with the manifest fails the release instead of
publishing a mismatched artifact.
