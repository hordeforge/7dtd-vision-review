# Threat Model

The CISO-facing view of deadeye's attack surface: what can be attacked, what
it costs, and what stands in the way. Enumerated from the code at the commit
below; every claim carries a file reference so the next pass can re-verify it.
Individual vulnerabilities are not fixed here — they are recorded as threats
and handed to sec-review.

- **Last reviewed:** 2026-08-25 (against `d9d70b5`)
- **Owner / review cadence:** organizational note — this document needs a
  named security owner and a review cadence; neither is defined in this
  repository yet. Re-review after any new provider adapter, the planned MCP
  server ([mcp-server.md](mcp-server.md)), or any change to `config.py`,
  `review.py`, or `intent.py`.

## Risk-ranked summary

| # | Sev | Threat | Where |
|---|-----|--------|-------|
| T1 | High | Config-supplied endpoint override forwards the provider API key and all media to an attacker-chosen host; cwd config shadows the home config | [T1](#t1-config-shadowed-credential-egress-high) |
| T2 | Medium | Intent-declared reference paths read arbitrary local files into the upload set, beyond the clip scope the operator consented to publish | [T2](#t2-intent-references-expand-the-upload-scope-medium) |
| T3 | Medium-Low | Credential hygiene rests on one name-based redaction control across every output path | [T3](#t3-single-redaction-backstop-medium-low) |
| T4 | Low | Intent size and reference count inflate billable prompt tokens; bounded by local caps since `intent.py` grew limits | [T4](#t4-cost-amplification-via-intent-low) |
| T5 | Low | Evidence envelopes are unsigned; integrity relies on filesystem controls alone | [T5](#t5-unsigned-evidence-low) |

Nothing here is internet-facing: deadeye is a local CLI with outbound-only
network access, gated on `--allow-network`. The highest-value target is the
**provider API key**; the highest-value data is **unreleased authored media**
plus its intent context.

## Assets

- **Provider API keys** (`GEMINI_API_KEY`/`GOOGLE_API_KEY`, `NVIDIA_API_KEY`,
  or values in `config.local.toml`): billing authority at third parties.
  Where they live and travel: [secrets flow](#secrets-flow).
- **Unreleased game media**: frames, muxed clips, reference assets. Leaving
  the machine is the disclosure event; governed by the provider's retention,
  not by anything here.
- **Intent documents**: authored context about unreleased content and where
  it lives in the tree.
- **Evidence envelopes**: hash-addressed records of what was reviewed and
  what was judged. Tampering one rewrites review history.
- **Provider quota/billing**: every real submission costs money.
- **Advisory trust**: a verdict is evidence, never sign-off
  (`ADVISORY_NOTE`, `src/deadeye/result.py:39`). A consumer gating on the
  verdict alone moves a human decision into a model.

## Entry points

No network listener exists; everything is invoked locally. The MCP server is
designed, not built ([mcp-server.md](mcp-server.md)).

| Entry point | Reference | Input |
|---|---|---|
| `deadeye review` flags | `src/deadeye/cli.py:52-93` | clip path, `--intent`/`--intent-text`, `--provider`, `--model`, `--allow-network`, `--json`, `--output`, `--keep-raw-response`, `--timeout`, `--force` |
| `deadeye doctor` / `schema` | `src/deadeye/cli.py:95-106` | none beyond env/config reads |
| Environment variables | `src/deadeye/config.py:32`; `providers/gemini.py:36`; `providers/nvidia.py:43` | `DEADEYE_CONFIG_DIR`, credential vars |
| TOML config files | `src/deadeye/config.py:59-82` | committed `config.toml` + gitignored `config.local.toml`; includes per-provider `endpoint` override |
| Clip media on disk | `src/deadeye/sampling.py:56-80` | frame files, muxed video, `client.log` (discovered only — see note below) |
| Intent JSON file / inline text | `src/deadeye/intent.py:177-189` | JSON validated against the intent schema |
| Intent `references[].path` | `src/deadeye/intent.py:150-162` | arbitrary filesystem paths → read and uploaded by `review.py:103-110,119-122,149-157` |
| Provider HTTP responses | `providers/gemini.py:128-189`, `providers/nvidia.py:131-187` | untrusted vendor payload over TLS |
| Outputs | `cli.py:149-160`; `evidence.py:128-144`; `review.py:131-141` | stdout JSON, evidence file, stderr disclosure lines |

Note: `sampling.discover()` finds `client.log` beside the frames
(`sampling.py:75`) but nothing ever submits or stores it — `review.py`
submits only sampled media plus intent references. SECURITY.md previously
claimed log contents leave the machine; that claim was false and is corrected
in this pass.

## Trust boundaries

```
B1 operator ──argv/env/cwd──> B2 filesystem inputs ──> process
process ──B3 egress (consent gate)──> provider API
provider API ──B4 TLS response──> validation ──> B5 outputs (stdout/evidence)
```

- **B1 → process**: same-user local trust. No authentication; anyone who can
  run the CLI spends the configured keys.
- **B2 → process**: clip files, intent documents, and both config files are
  read from disk without confinement. Discovery order makes **cwd config
  shadow the home config** (`config.py:59-66`), so a checked-out tree's
  `config.toml` wins over `~/.config/deadeye/`.
- **B3 egress**: exactly one gate — `allow_network` checked first of all in
  `run_review` (`review.py:66-71`), pinned by
  `tests/test_review.py:20-24`. Disclosure lines name provider, file count,
  byte total, and each submitted path (`review.py:132-141`) — but not the
  destination host.
- **B4 response**: vendor payload is untrusted input. Adapters extract text;
  `parse_model_json`/`validate_result` hard-fail on any deviation
  (`result.py:97-244`); no partial verdict survives a malformed response.
- **B5 outputs**: credentials must never reach stdout, JSON output, logs, or
  evidence; enforced by construction plus the `redact()` backstop
  (`intent.py:199-213`).

Privilege transitions: none in code (no privilege drop, spawn, or exec). Two
input-driven authority expansions exist and are modeled as threats: intent
references widen disk reads into the upload set (T2), and the endpoint
override redirects authenticated egress cross-host (T1).

### Secrets flow

Enter: environment or `config.local.toml` only — argparse defines no key flag
(`cli.py:38-107`); precedence in `config.credential_for`
(`config.py:151-168`). Live: process memory inside adapters. Leave: HTTP
header only — `x-goog-api-key` (`gemini.py:121-125`) or `Authorization:
Bearer` (`nvidia.py:123-128`), never a query string. Rotation: nothing in
this repository rotates, scopes, or revokes keys; that lives with whoever
holds the provider account.

## Threats per boundary

**Spoofing.** Provider endpoints are fixed HTTPS constants
(`gemini.py:35`, `nvidia.py:42`) verified by urllib's default TLS checks;
server impersonation reduces to T1 (redirect via config) or host/TLS
compromise. No caller authentication exists on B1 by design (local tool).

**Tampering.** cwd config shadowing lets repository-supplied TOML alter
provider, model, and endpoint (`config.py:59-66`, `config.toml` ships an
`endpoint` value) — T1. Evidence overwrite is refused without `--force` and
written atomically (`evidence.py:128-144`), but envelopes carry no signature:
post-write tampering is undetectable here — T5.

**Repudiation.** A run leaves no trace unless `--output` was given; the only
audit surface is the optional evidence envelope plus stderr disclosure lines.
No run ledger exists (noted for readiness; o11y-review owns log structure).

**Information disclosure.** Key leakage paths (stdout, evidence, raw
response) all funnel through one name-based backstop — T3. Provider error
bodies (≤300 chars) surface in refusal messages (`gemini.py:136-138`,
`nvidia.py:139-141`). `--intent-text` content is visible in process listings
(authored context, not credentials).

**Denial of service.** Local and bounded: byte budget enforced before any
read-for-submission (`review.py:124-129`), frame caps via sampling
(`sampling.py:171-180`), default timeout 120s (`cli.py:135`), no retry
loops. Residual cost amplification via intent size is T4. There is no remote
trigger for resource exhaustion; the CLI does nothing until a human runs it.

**Elevation of privilege.** None modeled: stdlib-only, no subprocess, no
eval, single process. Nearest analog is T2 (reading files the operator did
not mean to publish), which stays within the invoking user's own read
permissions.

## Mitigations that exist

| Control | Covers | Reference |
|---|---|---|
| Consent gate runs before credential reads and any contact | all egress (I, R) | `review.py:66-71`; pinned by `tests/test_review.py:20-24` |
| Credentials never accepted as arguments | argv/leakage (I) | `cli.py:38-107` (absence of any key flag) |
| Header-only credential transport | URL/access-log leakage (I) | `gemini.py:121-125`, `nvidia.py:123-128` |
| Name-based redaction backstop on params, usage, raw response | secret landing in evidence/stdout (I) | `intent.py:199-213`; applied at `evidence.py:113-117,124`, `review.py:202,236`; pinned by `tests/test_intent.py:84-96` |
| Vendor payload validated, refuse-not-coerce | hostile/malformed responses (T) | `result.py:97-244`; adapters extract text only |
| Local limits before submission: suffix allowlist, byte budget, frame cap | oversized/unexpected uploads (D) | `base.py:21-34`; `sampling.py:128-201`; `review.py:103-129` |
| Evidence no-overwrite by default, atomic write, SHA-256 addressing | history rewriting (T/R) | `evidence.py:128-144` |
| Endpoint override validated: https only, plain http loopback-only, refused before submission | cleartext credential egress via config (part of T1) | `config.py` `endpoint()`; pinned by `tests/test_config.py` endpoint tests |
| Config values validated at resolution: unknown `default_provider` and unusable timeout refused with named errors | silent wrong-provider / wrong-timeout operation (misconfiguration) | `cli.py` `_resolve_provider`/`_resolve_timeout`; pinned by `tests/test_config.py`, `tests/test_mcp.py` |
| Doctor reports presence only, never contacts a provider | capability probing used as an oracle (I) | `base.py:82-88`; `cli.py:179-209` |
| Zero runtime dependencies, bandit (S) lint rules armed | supply-chain surface | `pyproject.toml` |

Single point of failure: T3 — the redact backstop is the *only* control
standing between credentials and three output channels.

## Gaps (recorded for sec-review; not fixed here)

### T1: config-shadowed credential egress (High)

A writable `config.toml` (or `config.local.toml`, or `DEADEYE_CONFIG_DIR`)
that sets `default_provider` plus a per-provider `endpoint` redirects the
authenticated POST — bearer key and all media — to an attacker-chosen HTTPS
host. Discovery gives the checkout's own `config.toml` precedence over the
home directory (`config.py:59-66`), so a cloned tree supplies the redirect;
the override is read at submission time (`gemini.py:113-114`,
`nvidia.py:115-116`). Partially mitigated since this pass: `config.endpoint`
refuses any override that is not https:// (plain http survives only for a
loopback proxy such as `http://localhost:8080`, `config.py` `endpoint()`),
so the credential can no longer be walked onto a cleartext wire. What remains
open is host freedom: an attacker-named https:// domain passes, because a
valid-TLS impostor host satisfies the check. Consent is informed about the
act ("media leaves this machine for `<provider>`") but never names the
destination host (`review.py:138-141`). Enabling path: clone hostile repo →
victim runs `deadeye review ... --allow-network` from its root → env key sent
to the foreign endpoint. Candidate directions for sec-review: pin or warn on
endpoint overrides at submission time, name the resolved host in the
disclosure lines, or drop the override.

### T2: intent references expand the upload scope (Medium)

`references[].path` accepts any non-empty string path
(`intent.py:150-162`); existence and suffix are the only checks
(`review.py:104-110`) before the file is hashed and uploaded
(`review.py:119-122,149-157`). A crafted or mistaken intent makes deadeye
publish arbitrary readable files (e.g. outside the clip directory) once
consent is given. Partially mitigated: suffix allowlist, byte budget, and
disclosure lines naming every submitted path. See abuse case A1.

### T3: single redaction backstop (Medium-Low)

Redaction matches credential-ish *key names* (`intent.py:36-44`); a secret
under any other name passes into evidence, stdout JSON, or the preserved raw
response. The usage block deliberately keeps `token`-named billing counters
(`evidence.py:37-41`), narrowing the rule there on purpose. One control,
three high-impact output channels.

### T4: cost amplification via intent (Low)

`build_prompt` interpolates every intent field verbatim
(`prompt.py:54-74`), so oversized input inflates billable tokens. Bounded in
the same pass that wrote this note: `intent.py` caps each free-text field at
2,000 characters, `avoid`/`questions` at 32 entries of 500 characters each,
and `references` at 8 files (pinned by `tests/test_intent.py`), and the gemini
adapter caps output with `maxOutputTokens`. A multi-megabyte `--intent-text`
is now refused locally instead of being priced at the provider.

### T5: unsigned evidence (Low)

Envelopes are hash-addressed but not signed (`evidence.py:58-125`): the
SHA-256 values attest to submitted bytes, not to the envelope itself. Any
writer can alter a stored verdict after the fact; detection depends entirely
on external integrity controls.

## Abuse cases

- **A1 — exfiltration by intent.** A hostile or careless `intent.json`
  declares references pointing at files outside the clip directory
  (`{"path": "../../private.png", "purpose": "..."}`). Path named, so the
  operator sees it in the stderr disclosure — if reading. Code path:
  `intent.py:150-162` → `review.py:104-110` → upload at
  `review.py:149-157`.
- **A2 — spend gaming.** Inline `--intent-text` of arbitrary size or hundreds
  of questions would inflate the billed prompt (`cli.py:64` →
  `intent.py:186-189` → `prompt.py:54-74`); the local caps added with T4
  (field, list, and reference limits in `intent.py`) refuse it before
  submission.
- **A3 — credential capture via cloned config.** The T1 scenario: malicious
  checkout ships `config.toml` overriding provider and endpoint; operator's
  environment key authenticates the attacker's endpoint. Consent was given,
  destination was never shown.
- **Client-side enforcement trust.** Consumers gate on exit code and the
  validated result; the only guard against treating a verdict as acceptance
  is the advisory note riding the envelope (`result.py:39-43`,
  `architecture.md`). A consumer ignoring it converts advisory evidence into
  an automated gate — documented as a security decision, enforced nowhere in
  code.

## Response readiness (notes only)

- Reporting today is public GitHub issues only; SECURITY.md says so honestly.
  There is no documented path from "vulnerability reported" to "fix shipped".
- Runs leave no audit trail beyond optional `--output` envelopes; incident
  investigation would start from whatever evidence files exist on disk.
- CI's test job holds no provider credentials and contacts no provider
  (offline suite); the only secret in `.github/workflows/ci.yml` is the
  workflow-scoped `GITHUB_TOKEN`, confined to the separate badge-push job on
  main. Live-provider tests are opt-in via `DEADEYE_NETWORK_TESTS` and
  excluded from the default suite.
