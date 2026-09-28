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
library. Session handling is deliberately minimal: initialize/ping/tools,
nothing stateful beyond the protocol handshake.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO, TypeVar

from . import __version__
from ._streams import bind_process_output
from .errors import DeadeyeError, EvidenceWriteError
from .evidence import sha256_bytes
from .review import run_review as run_review_core
from .surface import (
    PROVIDERS,
    _resolve_provider,
    _resolve_timeout,
    build_preview_prompt,
    provider_states,
    schema_document,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "deadeye"
# One JSON-RPC frame is a path plus a small intent document, never media.
# Without a cap the long-lived stdio loop retains whatever a client writes
# until the next newline, so a missing delimiter (or a multi-megabyte
# `intent_text`) becomes an unbounded allocation. One MiB is far above any
# honest tools/call and still small enough to refuse before the process
# grows with the input.
_MAX_FRAME_BYTES = 1 * 1024 * 1024
_READ_CHUNK_BYTES = 8192
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

# Client-named keys to the completed reviews that answered them, oldest
# first: `key -> (call fingerprint, envelope, evidence-write fault, retained
# bytes)`. A review that was submitted and billed lands here whether or not
# its evidence reached disk, so a retry under the same key replays the same
# answer instead of billing the same media twice. A local refusal and an
# ambiguous timeout stay out: nothing completed, so the call is retryable.
# The retained size rides with the entry rather than in a separate running
# total, so clearing the ledger (what the test fixture does between cases) is
# enough to release everything it held.
_COMPLETED: OrderedDict[str, tuple[str, dict[str, Any], str | None, int]] = OrderedDict()

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
        "same arguments returns the first attempt's envelope without "
        "submitting anything, for the lifetime of this server process. That "
        "holds for a review whose evidence file could not be written: it was "
        "billed, so a repeat replays the same error and envelope instead of "
        "submitting the media a second time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip": {"type": "string", "description": "clip directory or video file"},
                "intent": {"type": "string", "description": "intent JSON file path"},
                "intent_text": {"type": "string", "description": "inline intent JSON"},
                "provider": {"type": "string", "description": "provider name (default per config)"},
                "model": {"type": "string"},
                "allow_network": {"type": "boolean", "description": "explicit upload consent"},
                "timeout_seconds": {"type": "number"},
                "keep_raw_response": {"type": "boolean"},
                "output": {"type": "string", "description": "evidence path"},
                "force": {"type": "boolean"},
                "idempotency_key": {
                    "type": "string",
                    "description": "client-chosen name for this logical operation; a "
                    "repeat of the same key with the same arguments returns the first "
                    "result instead of submitting again, including a review that "
                    "completed but could not write its evidence",
                },
            },
            "required": ["clip", "allow_network"],
        },
    },
    {
        "name": "doctor",
        "description": "Report provider capability state without contacting any provider.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "schema",
        "description": "The intent and result schemas as JSON.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prompt",
        "description": "Render the exact reviewer instruction the gateway injects "
        "for an intent, without running a review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "intent_text": {"type": "string"},
                "clip": {"type": "string"},
            },
            "required": [],
        },
    },
]


def _tool_result(payload: Any) -> dict[str, Any]:
    """A successful tool result: text content carrying the JSON payload."""
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {message}"}], "isError": True}


def _optional_boolean(params: dict[str, Any], name: str) -> bool:
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
        raise DeadeyeError(f"review parameter {name!r} must be a boolean")
    return value


def _call_review(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("allow_network") is not True:
        raise DeadeyeError(
            "review uploads the clip to a third party; pass allow_network=true "
            "as a JSON boolean to consent"
        )
    keep_raw_response = _optional_boolean(params, "keep_raw_response")
    force = _optional_boolean(params, "force")
    key = _idempotency_key(params)
    provider_name = _resolve_provider(params.get("provider"))
    # Same resolution and validation as the CLI flag: the tool argument, else
    # config's timeout_seconds, else the built-in default.
    timeout = _resolve_timeout(params.get("timeout_seconds"))
    output = Path(params["output"]) if params.get("output") else None

    if key is not None:
        replayed = _replayed_result(key, params)
        if replayed is not None:
            envelope, write_fault = replayed
            if write_fault is not None:
                # The first attempt submitted, billed, and then failed to
                # persist its evidence. `handle_frame` renders the replay
                # exactly as it rendered the original: the same fault, the
                # same envelope, and no second submission.
                raise EvidenceWriteError(write_fault, document=envelope)
            # The first attempt's verdict, verbatim: a duplicate call must not
            # submit the media again, and must not invent a second envelope
            # either. `created_utc` and `review_id` in the payload are the
            # first attempt's, which is what makes the replay auditable.
            return envelope

    def notify(line: str) -> None:
        # The CLI's disclosure contract carries over verbatim: what will
        # leave the machine is announced on stderr before submission, and
        # stdout stays protocol-only.
        print(line, file=sys.stderr)

    try:
        envelope = run_review_core(
            Path(params["clip"]),
            provider=PROVIDERS[provider_name](),
            intent_path=Path(params["intent"]) if params.get("intent") else None,
            intent_text=params.get("intent_text"),
            model=params.get("model"),
            allow_network=True,
            timeout_seconds=timeout,
            keep_raw_response=keep_raw_response,
            output=output,
            force=force,
            notify=notify,
        )
    except EvidenceWriteError as exc:
        if key is not None:
            _remember_result(key, params, exc.document, write_fault=str(exc))
        raise
    if key is not None:
        _remember_result(key, params, envelope)
    return envelope


def _idempotency_key(params: dict[str, Any]) -> str | None:
    """The client's key for this logical operation, or None when it named none."""
    if "idempotency_key" not in params:
        return None
    key = params["idempotency_key"]
    if not isinstance(key, str) or not key.strip():
        raise DeadeyeError("review parameter 'idempotency_key' must be a non-empty string")
    if len(key) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise DeadeyeError(
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


def _replayed_result(key: str, params: dict[str, Any]) -> tuple[dict[str, Any], str | None] | None:
    """The first attempt's envelope for `key` and its evidence-write fault,
    or None when there is none."""
    entry = _COMPLETED.get(key)
    if entry is None:
        return None
    fingerprint, envelope, write_fault, _ = entry
    if fingerprint != _call_fingerprint(params):
        # Returning the earlier envelope here would attribute one operation's
        # verdict to another's request, and re-running would bill a second
        # time under a name the client already used. Refuse instead.
        raise DeadeyeError(
            f"idempotency_key {key!r} was already used for a review with different "
            "arguments; a key names one logical operation, so pass a new one"
        )
    _COMPLETED.move_to_end(key)
    return envelope, write_fault


def _remember_result(
    key: str, params: dict[str, Any], envelope: dict[str, Any], *, write_fault: str | None = None
) -> None:
    """Record a completed review under `key`, evicting the oldest past either bound."""
    # Measured the way the envelope travels: the ledger holds what a client
    # would have been sent, so the retained size is the rendered size. A
    # non-serializable leaf is a bug the frame loop reports, not a reason to
    # lose the record of a billed submission, so it falls back to the key's
    # own size rather than raising here.
    try:
        retained = len(json.dumps(envelope, sort_keys=True).encode("utf-8"))
    except (TypeError, ValueError):
        retained = len(key.encode("utf-8"))
    _COMPLETED[key] = (_call_fingerprint(params), envelope, write_fault, retained)
    _COMPLETED.move_to_end(key)
    while len(_COMPLETED) > 1 and (
        len(_COMPLETED) > _IDEMPOTENCY_LEDGER_ENTRIES
        or sum(entry[3] for entry in _COMPLETED.values()) > _IDEMPOTENCY_LEDGER_MAX_BYTES
    ):
        _COMPLETED.popitem(last=False)


def _call_doctor(params: dict[str, Any]) -> dict[str, Any]:
    # The same per-provider state `deadeye doctor --json` prints, from the same
    # single home in surface.py: where the credential came from, never its
    # value. The JSON-RPC tool result wraps that array under a `providers` key
    # so one tool result stays a JSON object.
    return {"providers": provider_states()}


def _call_schema(params: dict[str, Any]) -> dict[str, Any]:
    return schema_document()


def _call_prompt(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": build_preview_prompt(
            Path(params["intent"]) if params.get("intent") else None,
            params.get("intent_text"),
            Path(params["clip"]) if params.get("clip") else None,
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
    request_id = frame.get("id")
    if request_id is None:
        return None  # notification (e.g. notifications/initialized)
    method = frame.get("method")
    if not isinstance(method, str):
        return _error(request_id, -32600, "Invalid Request")
    params = frame.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": _initialize_result()}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(
                request_id, -32602, "Invalid params: name must be a string and arguments an object"
            )
        call = _CALLS.get(name)
        if call is None:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(call(arguments))}
        except EvidenceWriteError as exc:
            # Same contract as the CLI: the submission completed and was
            # billed, only the evidence write failed. isError stays true
            # (nothing was persisted), and the full envelope travels in the
            # result text so an agent recovers it without resubmitting.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
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
                },
            }
        except DeadeyeError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": _tool_error(str(exc))}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            # A bare KeyError's str is just the quoted key ('clip'), which
            # names neither the tool nor the fault; keep the type and tool on
            # the record so the client sees what argument was missing.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_error(f"tool {name!r} failed: {type(exc).__name__}: {exc}"),
            }
    return _error(request_id, -32601, "Method not found")


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
    }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


_Line = TypeVar("_Line", bytes, str)


def _discard_through_newline(read: Callable[[int], Any], newline: _Line) -> _Line:
    """Drop bytes until (and including) the next newline; return what follows."""
    empty: _Line = newline[:0]
    while True:
        chunk = read(_READ_CHUNK_BYTES)
        if not chunk:
            return empty
        if not isinstance(chunk, type(newline)):
            return empty
        index = chunk.find(newline)
        if index >= 0:
            return chunk[index + len(newline) :]


def _split_stdio_frames(
    read: Callable[[int], Any],
    first: _Line,
    newline: _Line,
    max_bytes: int,
) -> Iterator[_Line | None]:
    """Chunked newline split that never retains more than `max_bytes` of a frame.

    `None` means the current frame exceeded the cap and was discarded through
    its terminating newline (or EOF), so the next yield is still aligned.
    """
    leftover: _Line = first
    while True:
        while True:
            index = leftover.find(newline)
            if index < 0:
                break
            line, leftover = leftover[:index], leftover[index + len(newline) :]
            yield None if len(line) > max_bytes else line
        if len(leftover) > max_bytes:
            yield None
            leftover = _discard_through_newline(read, newline)
            continue
        chunk = read(_READ_CHUNK_BYTES)
        if not chunk:
            if leftover:
                yield None if len(leftover) > max_bytes else leftover
            return
        if not isinstance(chunk, type(leftover)):
            return
        leftover += chunk


def _frame_size(payload: bytes | str) -> int:
    """A frame's size in the bytes `_MAX_FRAME_BYTES` is named in.

    The stdio transport is bytes, so bytes is the unit the cap counts there.
    A text frame reaches the same cap through a different door, and measuring
    it in code points would admit a frame of four-byte characters at four
    times the intended size. `surrogatepass` keeps the measure total over
    every `str`, so a lone surrogate in a text source cannot raise here
    either; the bytes transport cannot carry one, and refusing the frame it
    belongs to is the transport's business, not the counter's.
    """
    if isinstance(payload, bytes):
        return len(payload)
    return len(payload.encode("utf-8", "surrogatepass"))


def _iter_pre_split_frames(source: Iterable[Any], max_bytes: int) -> Iterator[bytes | str | None]:
    """Bound frames that already arrive one line at a time (a list, a test double)."""
    for raw_line in source:
        if isinstance(raw_line, bytes):
            payload: bytes | str = raw_line.removesuffix(b"\n")
        else:
            payload = raw_line.removesuffix("\n")
        yield None if _frame_size(payload) > max_bytes else payload


def _iter_stdio_frames(source: Any, max_bytes: int) -> Iterator[bytes | str | None]:
    """One raw newline-delimited frame at a time, or None when a frame is oversized."""
    read = getattr(source, "read", None)
    if not callable(read):
        yield from _iter_pre_split_frames(source, max_bytes)
        return
    first = read(_READ_CHUNK_BYTES)
    if not first:
        return
    newline: bytes | str = b"\n" if isinstance(first, bytes) else "\n"
    yield from _split_stdio_frames(read, first, newline, max_bytes)


def _write_frame(stdout: TextIO, frame: dict[str, Any]) -> None:
    """Write one response frame and flush it; the transport is unbuffered."""
    print(json.dumps(frame), file=stdout)
    stdout.flush()


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
    for raw_line in _iter_stdio_frames(source, _MAX_FRAME_BYTES):
        if raw_line is None:
            _write_frame(stdout, _error(None, -32700, "Parse error"))
            continue
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                _write_frame(stdout, _error(None, -32700, "Parse error"))
                continue
        else:
            line = raw_line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            # A frame nested beyond the interpreter limit is malformed input,
            # not a fault in this loop: it gets the spec's parse error like
            # any other malformed frame instead of killing the transport
            # (the same treatment intent.py gives such documents).
            _write_frame(stdout, _error(None, -32700, "Parse error"))
            continue
        if not isinstance(frame, dict):
            _write_frame(stdout, _error(None, -32600, "Invalid Request"))
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
            _write_frame(stdout, response)
    return 0
