"""Minimal Model Context Protocol server for deadeye (stdio transport).

Same contract, different transport: the MCP tools map onto the CLI surface
(`review`, `doctor`, `schema`, `prompt`) and return the same shapes, so an
MCP client (an agent, a dashboard, a control script) reaches the gateway over
standard JSON-RPC instead of a subprocess. No second result format, no new
authority model.

The boundaries from the CLI do not weaken:

- `review` takes an explicit `allow_network` parameter and refuses the upload
  without it, exactly like `--allow-network`.
- Credentials still come from the environment or loaded configuration
  (normally `config.local.toml`); disclosure lines go to stderr; the
  redaction backstop still applies.
- stdout is the JSON-RPC channel: nothing here prints to stdout except the
  framed responses.
- Fail closed: malformed frames get spec JSON-RPC errors, unknown tools
  error, and an invalid review call refuses without a partial verdict.

Transport: newline-delimited JSON-RPC 2.0 on stdio, per the MCP spec. No
third-party SDK; the protocol surface is small enough to keep in the standard
library. The framing itself (chunked splitting, the frame cap, the write) is
`_jsonrpc_frames`; this module owns the tools and the dispatch over them.
Session handling is deliberately minimal: initialize/ping/tools, and
the one piece of session state a client can ask for, the bounded
`idempotency_key` ledger below.
"""

from __future__ import annotations

import json
import sys
import traceback
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, overload

from . import __version__, _jsonrpc_frames
from ._streams import bind_process_output
from .errors import DeadeyeError, EvidenceWriteError, NoVerdictError, UsageError
from .evidence import sha256_bytes
from .review import run_review as run_review_core
from .surface import (
    PROVIDERS,
    build_preview_prompt,
    config_diagnosis,
    provider_states,
    resolve_provider,
    resolve_timeout,
    schema_document,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "deadeye"
# The only characters that make an incoming line blank: the four RFC 8259
# section 2 calls insignificant whitespace, and the only ones a frame may
# carry around its JSON without changing it. Blank-line detection must not
# use `str.strip()`: with no argument that also removes U+001C..U+001F,
# U+0085, U+2028 and U+3000, so a line holding nothing but one of those would
# be discarded in silence instead of answered with the spec's parse error, and
# a client that wrote a frame and waits for one waits forever. Any other byte
# belongs to the frame, so a line that survives their removal is answered: a
# parse error when it is malformed.
_JSON_WHITESPACE = " \t\r\n"
# How many completed `review` results a client-named idempotency key holds.
# A replay is answered from here instead of submitted again, so the ledger
# must be bounded: the server is long-lived and a key is a client-chosen
# string. Least-recently-used eviction, process-local: a restart drops the
# ledger, which is why the tool description tells a client the guarantee
# covers transport replay within one session, not across restarts.
_IDEMPOTENCY_LEDGER_ENTRIES = 128
# The entry count is not the only thing a retained envelope costs. One entry
# carries the whole evidence envelope, and with `keep_raw_response` that
# includes the redacted provider payload, which `_http` bounds at 8 MiB per
# response: 128 of those is a gigabyte pinned in a process that is meant to
# idle between reviews. A byte budget is the second bound, and it is the one
# that scales with what a client asks for rather than with how many keys it
# happens to name. The newest entry is always kept whatever it weighs, so a
# key that has just been answered still replays instead of billing twice; a
# single entry is bounded already by the response cap and the prompt caps.
_IDEMPOTENCY_LEDGER_MAX_BYTES = 32 * 1024 * 1024
# Long enough to name a job and its asset, short enough that the key stays a
# log line. Anything longer is a client bug, not a key.
_MAX_IDEMPOTENCY_KEY_CHARS = 200

# Client-named keys to the submissions that spent them, oldest first. A
# review that reached the provider lands here whether or not it produced a
# verdict, so a retry under the same key replays the same answer instead of
# billing the same media twice. A local refusal stays out: nothing was
# submitted, so the call is retryable.
# The retained size rides with the entry rather than in a separate running
# total, so clearing the ledger (what the test fixture does between cases) is
# enough to release everything it held.
_COMPLETED: OrderedDict[str, _LedgerEntry] = OrderedDict()


@dataclass(frozen=True)
class _LedgerEntry:
    """What one key spent: the answer a repeat must replay, and what it cost.

    `envelope` is the completed verdict when one came back. `fault` is the
    refusal a repeat re-raises instead, and is set both for a verdict that
    could not be written to disk (the envelope rides alongside it) and for a
    submission that was billed and answered nothing usable, where there is no
    envelope to keep.
    """

    fingerprint: str
    envelope: dict[str, Any] | None
    fault: str | None
    retained_bytes: int


def _intent_route_schema() -> dict[str, Any]:
    """The exactly-one intent rule as the tool schemas publish it.

    `load_intent` refuses a call that names neither route and one that names
    both, but `required` alone can only say "at least these", so a client
    generating a call from the published schema would build a `clip`-only
    review and collect the refusal at call time instead of reading the rule
    where it reads every other argument. The `not` halves are what make it
    "exactly one" rather than "one or the other".
    """
    return {
        "oneOf": [
            {"required": ["intent"], "not": {"required": ["intent_text"]}},
            {"required": ["intent_text"], "not": {"required": ["intent"]}},
        ]
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "review",
        "description": "Submit a clip (frame directory or muxed video) plus its "
        "recorded intent to a vision model and return the advisory evidence "
        "envelope. Uploads the clip to a third party: refuses without "
        "allow_network=true. Without an idempotency_key, every call is one "
        "new billable submission and never retries: resending this call after "
        "a lost response or a timeout submits the media again rather than "
        "replaying the first attempt. Supply idempotency_key to name the "
        "logical operation instead: a repeated call with the same key and the "
        "same arguments returns the first attempt's answer without submitting "
        "anything, for the lifetime of this server process. That holds for "
        "every submission that reached the provider, billed or not: a review "
        "whose evidence file could not be written replays that error and its "
        "envelope, and a review the provider answered with nothing usable (a "
        "timeout, an answer the adapter cannot use, an envelope no verdict "
        "can be read out of, a response that failed validation) replays the "
        "same refusal rather than submitting the media a second time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip": {
                    "type": "string",
                    "minLength": 1,
                    "description": "clip directory or video file",
                },
                "intent": {
                    "type": "string",
                    "minLength": 1,
                    "description": "intent JSON file path; exactly one of intent or "
                    "intent_text, never both and never neither",
                },
                "intent_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "inline intent JSON; exactly one of intent or "
                    "intent_text, never both and never neither",
                },
                "provider": {
                    "type": "string",
                    "enum": sorted(PROVIDERS),
                    "minLength": 1,
                    "description": "provider name (default per config)",
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": "provider model id (default per provider)",
                },
                "allow_network": {"type": "boolean", "description": "explicit upload consent"},
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "positive seconds to wait for the provider; zero, "
                    "a negative number, and a non-number are refused before submission",
                },
                "keep_raw_response": {
                    "type": "boolean",
                    "description": "retain a redacted raw response in evidence",
                },
                "output": {
                    "type": "string",
                    "minLength": 1,
                    "description": "evidence path",
                },
                "force": {
                    "type": "boolean",
                    "description": "overwrite an earlier envelope at output",
                },
                "idempotency_key": {
                    "type": "string",
                    "maxLength": _MAX_IDEMPOTENCY_KEY_CHARS,
                    "description": "client-chosen name for this logical operation; a "
                    "repeat of the same key with the same arguments returns the first "
                    "result instead of submitting again, including a review that "
                    "completed but could not write its evidence and one the provider "
                    "answered with no usable verdict",
                },
            },
            "required": ["clip", "allow_network"],
            "additionalProperties": False,
            **_intent_route_schema(),
        },
    },
    {
        "name": "doctor",
        "description": "Report provider capability state and the effective "
        "configuration without contacting any provider.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "schema",
        "description": "The intent and result schemas as JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt",
        "description": "Render the exact reviewer instruction the gateway injects "
        "for an intent, without running a review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "minLength": 1,
                    "description": "intent JSON file path; exactly one of intent or "
                    "intent_text, never both and never neither",
                },
                "intent_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "inline intent JSON; exactly one of intent or "
                    "intent_text, never both and never neither",
                },
                "clip": {
                    "type": "string",
                    "minLength": 1,
                    "description": "optional clip directory or video file, described "
                    "in the rendered prompt's media summary",
                },
            },
            "required": [],
            "additionalProperties": False,
            **_intent_route_schema(),
        },
    },
]


def _tool_result(payload: Any) -> dict[str, Any]:
    """A successful tool result: text content carrying the JSON payload."""

    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}


def _error_code(exc: DeadeyeError) -> str:
    """The machine-readable kind of a tool refusal.

    A client holding an idempotency key has to tell a spent submission from a
    free one before it decides whether to retry, and the prose cannot carry
    that: `errors.py` types the refusals for exactly this reason, so the type
    is what travels here. `no_verdict` is the one that bills; `usage` and
    `refused` name caller's mistakes and a provider that refused before
    running the review, and both are retryable under the same key. Every
    refusal this module raises while reading an argument is a `UsageError`,
    the class argparse raises for the same mistake on the CLI, so a
    malformed call reads as `usage` here and exits 2 there.
    """
    if isinstance(exc, NoVerdictError):
        return "no_verdict"
    if isinstance(exc, UsageError):
        return "usage"
    return "refused"


def _tool_error(message: str, code: str) -> dict[str, Any]:
    """A refused tool call: the prose a person reads and the code a client
    branches on. The `ERROR:` prefix stays the one text contract every
    transport shares; the code rides `structuredContent`, the spec's
    machine-readable channel, so a client that ignores it loses nothing and a
    client that reads it does not have to match on message text."""

    return {
        "content": [{"type": "text", "text": f"ERROR: {message}"}],
        "isError": True,
        "structuredContent": {"error": {"code": code, "message": message}},
    }


def _boolean(tool: str, params: dict[str, Any], name: str) -> bool:
    """An optional MCP control flag, defaulting to false when absent.

    JSON strings are truthy in Python, so `bool(params[name])` would turn a
    client-supplied value such as ``"false"`` into permission to retain raw
    responses or overwrite an earlier evidence envelope. Control flags must
    therefore be literal JSON booleans at this protocol boundary.
    """
    if name not in params:
        return False
    value = params[name]
    if not isinstance(value, bool):
        raise UsageError(f"{tool} parameter {name!r} must be a boolean")
    return value


def _text(tool: str, params: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    """A path, an inline JSON document, or a model name as the client sent it.

    A JSON null is an absent argument, the same as leaving the key out: the
    optional routes read that way throughout. Anything else must be a string.
    A number or a boolean would otherwise reach `Path()` and fail there as a
    TypeError naming no argument, and a missing required one would surface as
    a bare KeyError; both are the caller's mistake, so both are named here.

    A blank string is refused for the same reason. `Path("")` is the current
    directory, so an empty `clip` would submit the server's working directory
    as the media and an empty `intent` would fail as an unreadable `.`, both
    reported as somebody else's fault, and an empty `model` would reach the
    provider as an empty model id. The published schema carries `minLength: 1`
    for these arguments; this is the half a schema cannot enforce.
    """
    value = params.get(name)
    if value is None:
        if required:
            raise UsageError(f"{tool} parameter {name!r} is required")
        return None
    if not isinstance(value, str):
        raise UsageError(f"{tool} parameter {name!r} must be a string")
    if not value.strip():
        raise UsageError(f"{tool} parameter {name!r} must not be empty")
    return value


@overload
def _path_arg(tool: str, params: dict[str, Any], name: str, *, required: Literal[True]) -> Path: ...


@overload
def _path_arg(
    tool: str, params: dict[str, Any], name: str, *, required: bool = False
) -> Path | None: ...


def _path_arg(
    tool: str, params: dict[str, Any], name: str, *, required: bool = False
) -> Path | None:
    value = _text(tool, params, name, required=required)
    return Path(value) if value is not None else None


def _provider_arg(name: Any) -> str:
    """The provider to submit to: the tool argument, else the configured default.

    `review --provider` is bounded by argparse's `choices`, so an unknown
    name is a usage error there. A JSON-RPC argument carries no such bound,
    and an unvalidated one reaches the provider registry as a `KeyError`,
    which reaches the client as an internal fault rather than the refusal a
    typo deserves.
    """
    if name is not None and not isinstance(name, str):
        raise UsageError("review parameter 'provider' must be a string")
    provider = resolve_provider(name)
    if provider not in PROVIDERS:
        raise UsageError(
            f"review parameter 'provider' {provider!r} is not one of {', '.join(sorted(PROVIDERS))}"
        )
    return provider


def _known_args(tool: str, params: dict[str, Any]) -> None:
    """Refuse an argument the tool's published schema does not declare.

    Every other argument at this boundary is read, typed, and named, so a
    misspelled one is the last way a call can go quietly wrong: `intetnt`
    instead of `intent` is dropped, and the client collects a refusal about
    the intent route it believes it supplied. The published properties are
    the list, so the schema and this check cannot drift apart. A name the
    schema does not declare is the caller's mistake, so it carries the
    `usage` code every other argument refusal carries, not the `refused` a
    provider refusal carries.
    """
    declared = next(item["inputSchema"]["properties"] for item in TOOLS if item["name"] == tool)
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise UsageError(
            f"{tool} does not take {', '.join(repr(name) for name in unknown)}; "
            f"it takes {', '.join(sorted(declared))}"
        )


def _call_review(params: dict[str, Any]) -> dict[str, Any]:
    _known_args("review", params)
    if params.get("allow_network") is not True:
        raise DeadeyeError(
            "review uploads the clip to a third party; pass allow_network=true "
            "as a JSON boolean to consent"
        )
    keep_raw_response = _boolean("review", params, "keep_raw_response")
    force = _boolean("review", params, "force")
    key = _idempotency_key(params)
    # Every argument is read and typed before anything is submitted, so a
    # malformed call is refused by the same envelope whether or not the
    # provider would have taken the request.
    clip = _path_arg("review", params, "clip", required=True)
    intent_path = _path_arg("review", params, "intent")
    intent_text = _text("review", params, "intent_text")
    model = _text("review", params, "model")
    output = _path_arg("review", params, "output")
    provider_name = _provider_arg(params.get("provider"))
    # Same resolution and validation as the CLI flag: the tool argument, else
    # config's timeout_seconds, else the built-in default.
    timeout = _timeout_arg(params)

    if key is not None:
        replayed = _replayed_result(key, params)
        if replayed is not None:
            if replayed.envelope is None:
                # The first attempt reached the provider and came back with
                # nothing usable, so there is no envelope to hand over. The
                # submission is spent either way: `handle_frame` renders this
                # replay exactly as it rendered the original, the same
                # refusal and no second submission.
                raise NoVerdictError(replayed.fault or "the earlier submission returned no verdict")
            if replayed.fault is not None:
                # The first attempt submitted, billed, and then failed to
                # persist its evidence. `handle_frame` renders the replay
                # exactly as it rendered the original: the same fault, the
                # same envelope, and no second submission.
                raise EvidenceWriteError(replayed.fault, document=replayed.envelope)
            # The first attempt's verdict, verbatim: a duplicate call must not
            # submit the media again, and must not invent a second envelope
            # either. `created_utc` and `review_id` in the payload are the
            # first attempt's, which is what makes the replay auditable.
            return replayed.envelope

    def notify(line: str) -> None:
        # The CLI's disclosure contract carries over verbatim: what will
        # leave the machine is announced on stderr before submission, and
        # stdout stays protocol-only.
        print(line, file=sys.stderr)

    try:
        envelope = run_review_core(
            clip,
            provider=PROVIDERS[provider_name](),
            intent_path=intent_path,
            intent_text=intent_text,
            model=model,
            allow_network=True,
            timeout_seconds=timeout,
            keep_raw_response=keep_raw_response,
            output=output,
            force=force,
            notify=notify,
        )
    except EvidenceWriteError as exc:
        if key is not None:
            _remember_result(key, params, envelope=exc.document, fault=str(exc))
        raise
    except NoVerdictError as exc:
        # The media left the machine and the attempt may be billed, so the
        # key is spent even though nothing came back. Recording it is what
        # stops a client retrying a lost answer into a second charge.
        if key is not None:
            _remember_result(key, params, fault=str(exc))
        raise
    if key is not None:
        _remember_result(key, params, envelope=envelope)
    return envelope


def _timeout_arg(params: dict[str, Any]) -> float:
    """The submission budget, validated as this transport's argument.

    `resolve_timeout` is the one home for the range check, so it runs for the
    value either way. What changes here is the refusal a client gets back: a
    value the caller put in the call is the caller's mistake, the same class
    argparse rejects `--timeout 0` with, so it carries the `usage` code. A
    value that came from configuration is a configuration refusal and keeps
    the `refused` the core raises.
    """
    if params.get("timeout_seconds") is None:
        return resolve_timeout(None)
    try:
        return resolve_timeout(params["timeout_seconds"])
    except UsageError:
        raise
    except DeadeyeError as exc:
        raise UsageError(str(exc)) from exc


def _idempotency_key(params: dict[str, Any]) -> str | None:
    """The client's key for this logical operation, or None when it named none.

    The ledger lookup is an identity comparison, so the key is normalized to
    NFC before it is used as one. A key typed on macOS (or pasted from a
    decomposed source) arrives with combining marks where the composed
    spelling has precomposed ones, and both spell the same logical operation;
    as two ledger entries they answer two different questions, so the retry
    the key exists to prevent submitted the media a second time and billed it
    again. NFC is the form a client is most likely to have typed, and folding
    to it loses nothing a key can carry: it is an opaque name, never rendered
    back to the client, and the length cap is applied after the fold so a key
    cannot shrink its way past it.
    """
    if "idempotency_key" not in params:
        return None
    key = params["idempotency_key"]
    if not isinstance(key, str) or not key.strip():
        raise UsageError("review parameter 'idempotency_key' must be a non-empty string")
    key = unicodedata.normalize("NFC", key)
    if len(key) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise UsageError(
            f"review parameter 'idempotency_key' must be at most "
            f"{_MAX_IDEMPOTENCY_KEY_CHARS} characters"
        )
    return key


def _call_fingerprint(params: dict[str, Any]) -> str:
    """A digest of the call as sent, minus the key itself.

    Two calls sharing a key must be the same operation. Comparing the raw
    arguments (not the resolved ones) is what the client controls: a resend
    is byte-identical by definition, and a fingerprint over resolved values
    would change under a config edit between the two calls, which is a
    different operation wearing the same name.
    """
    call = {name: value for name, value in params.items() if name != "idempotency_key"}
    return sha256_bytes(json.dumps(call, sort_keys=True).encode("utf-8"))


def _replayed_result(key: str, params: dict[str, Any]) -> _LedgerEntry | None:
    """The first attempt's ledger entry for `key`, or None when there is none."""
    entry = _COMPLETED.get(key)
    if entry is None:
        return None
    if entry.fingerprint != _call_fingerprint(params):
        # Returning the earlier answer here would attribute one operation's
        # verdict to another's request, and re-running would bill a second
        # time under a name the client already used. Refuse instead. The
        # second call is the caller's mistake and the key is unspent, so the
        # refusal is a usage error and a corrected retry under a new key is
        # what it asks for.
        raise UsageError(
            f"idempotency_key {key!r} was already used for a review with different "
            "arguments; a key names one logical operation, so pass a new one"
        )
    _COMPLETED.move_to_end(key)
    return entry


def _remember_result(
    key: str,
    params: dict[str, Any],
    *,
    envelope: dict[str, Any] | None = None,
    fault: str | None = None,
) -> None:
    """Record a submission under `key`, evicting the oldest past either bound."""
    # Measured the way the answer travels: the ledger holds what a client
    # would have been sent, so the retained size is the rendered size. A
    # non-serializable leaf is a bug the frame loop reports, not a reason to
    # lose the record of a billed submission, so it falls back to the key's
    # own size rather than raising here.
    try:
        retained = len(json.dumps(envelope, sort_keys=True).encode("utf-8"))
    except (TypeError, ValueError):
        retained = len(key.encode("utf-8"))
    if fault is not None:
        retained += len(fault.encode("utf-8"))
    _COMPLETED[key] = _LedgerEntry(
        fingerprint=_call_fingerprint(params),
        envelope=envelope,
        fault=fault,
        retained_bytes=retained,
    )
    _COMPLETED.move_to_end(key)
    # The ledger's weight is totaled once, not once per eviction: the loop
    # condition re-summed every entry on every pass, so a run of evictions
    # cost a pass over the whole ledger each time. Recomputing it here rather
    # than carrying it between calls is what keeps a ledger a test cleared
    # directly from desynchronizing the bound.
    total = sum(entry.retained_bytes for entry in _COMPLETED.values())
    while len(_COMPLETED) > 1 and (
        len(_COMPLETED) > _IDEMPOTENCY_LEDGER_ENTRIES or total > _IDEMPOTENCY_LEDGER_MAX_BYTES
    ):
        total -= _COMPLETED.popitem(last=False)[1].retained_bytes


def _call_doctor(params: dict[str, Any]) -> dict[str, Any]:
    _known_args("doctor", params)
    # The same per-provider state `deadeye doctor --json` prints, and the same
    # effective-configuration diagnosis it prints beside it, from the same
    # single home in surface.py: where the credential came from, never its
    # value. The JSON-RPC tool result wraps them under `providers` and `config`
    # so one tool result stays a JSON object.
    return {"providers": provider_states(), "config": config_diagnosis()}


def _call_schema(params: dict[str, Any]) -> dict[str, Any]:
    _known_args("schema", params)
    return schema_document()


def _call_prompt(params: dict[str, Any]) -> dict[str, Any]:
    _known_args("prompt", params)
    return {
        "prompt": build_preview_prompt(
            _path_arg("prompt", params, "intent"),
            _text("prompt", params, "intent_text"),
            _path_arg("prompt", params, "clip"),
        )
    }


_CALLS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "review": _call_review,
    "doctor": _call_doctor,
    "schema": _call_schema,
    "prompt": _call_prompt,
}


def handle_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC request/notification; None for a notification."""
    # The spec separates the two by the presence of the member, not by its
    # value: a frame carrying `"id": null` is a request whose id is null and
    # gets an answer, and dropping it silently would leave the client waiting
    # on a reply that never comes.
    if "id" not in frame:
        return None  # notification (e.g. notifications/initialized)
    request_id = frame["id"]
    version = frame.get("jsonrpc")
    if version is not None and version != "2.0":
        # A member that names a different protocol version is a frame this
        # server does not speak, which the spec classes as an invalid request.
        # An absent member is served: the version is the one thing a lenient
        # client may leave out without changing what the frame asks for, and
        # refusing it would buy nothing a wrong version does not already cost.
        return _error(request_id, -32600, "Invalid Request: the jsonrpc member must be '2.0'")
    method = frame.get("method")
    if not isinstance(method, str):
        return _error(request_id, -32600, "Invalid Request")
    params = frame.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        return _result(request_id, _initialize_result())
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(
                request_id, -32602, "Invalid params: name must be a string and arguments an object"
            )
        call = _CALLS.get(name)
        if call is None:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        try:
            return _result(request_id, _tool_result(call(arguments)))
        except EvidenceWriteError as exc:
            # Same contract as the CLI: the submission completed and was
            # billed, only the evidence write failed. isError stays true
            # (nothing was persisted), and the full envelope travels in the
            # result text so an agent recovers it without resubmitting.
            return _result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"error": str(exc), "envelope": exc.document},
                                indent=2,
                                sort_keys=True,
                            ),
                        }
                    ],
                    "isError": True,
                    "structuredContent": {
                        "error": {"code": "evidence_write", "message": str(exc)},
                        "envelope": exc.document,
                    },
                },
            )
        except DeadeyeError as exc:
            return _result(request_id, _tool_error(str(exc), _error_code(exc)))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            # A bare KeyError's str is just the quoted key ('clip'), which
            # names neither the tool nor the fault; keep the type and tool on
            # the record so the client sees what argument was missing. `fault`
            # is its own code because nothing here is known to be the caller's
            # mistake, so a client must not read it as a refusal the same key
            # could retry.
            return _result(
                request_id,
                _tool_error(f"tool {name!r} failed: {type(exc).__name__}: {exc}", "fault"),
            )
    return _error(request_id, -32601, "Method not found")


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(
    stdin: Iterable[str | bytes] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """The stdio loop: one JSON-RPC frame per line, responses on stdout.

    `stdin` is an iterable of lines, text or bytes (tests pass StringIO or
    BytesIO); `stdout` is the text stream every response is written to.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    # The verdict rides a JSON-RPC payload, and a fault trace rides stderr;
    # neither may die in `print` under a C or POSIX locale once the review has
    # been billed (see `_streams`). Injected streams are left alone: the
    # caller owns the encoding of the object it passed in.
    if stdout is sys.stdout:
        bind_process_output()
    # The transport is UTF-8 JSON, so read bytes when the stream exposes them:
    # a frame with an invalid byte must get the spec's parse error like any
    # other malformed frame, not kill the loop inside the text iterator.
    source = getattr(stdin, "buffer", stdin)
    frames = _jsonrpc_frames.iter_stdio_frames(source, _jsonrpc_frames.MAX_FRAME_BYTES)
    for raw_line in frames:
        if raw_line is None:
            _jsonrpc_frames.write_frame(stdout, _error(None, -32700, "Parse error"))
            continue
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8").strip(_JSON_WHITESPACE)
            except UnicodeDecodeError:
                _jsonrpc_frames.write_frame(stdout, _error(None, -32700, "Parse error"))
                continue
        else:
            line = raw_line.strip(_JSON_WHITESPACE)
        if not line:
            continue
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            # A frame nested beyond the interpreter limit is malformed input,
            # not a fault in this loop: it gets the spec's parse error like
            # any other malformed frame instead of killing the transport
            # (the same treatment intent.py gives such documents).
            _jsonrpc_frames.write_frame(stdout, _error(None, -32700, "Parse error"))
            continue
        if not isinstance(frame, dict):
            # A batch is an array of requests, and this transport takes one
            # frame per line. Naming the reason beats a bare "Invalid
            # Request": a client that batches otherwise reads the refusal as a
            # server fault and retries the same shape forever.
            message = (
                "Invalid Request: JSON-RPC batching is not supported; send one request per line"
                if isinstance(frame, list)
                else "Invalid Request"
            )
            _jsonrpc_frames.write_frame(stdout, _error(None, -32600, message))
            continue
        try:
            response = handle_frame(frame)
        except Exception:  # noqa: BLE001
            # One faulty frame must not tear down the transport: answer the
            # spec's internal-error code and keep serving, with the trace on
            # stderr (stdout stays protocol-only). Justified broad catch: this
            # is the per-frame isolation boundary of a long-lived server.
            traceback.print_exc(file=sys.stderr)
            response = _error(frame.get("id"), -32603, "Internal error")
        if response is not None:
            _jsonrpc_frames.write_frame(stdout, response)
    return 0
