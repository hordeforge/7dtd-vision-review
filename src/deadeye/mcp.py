"""Minimal Model Context Protocol server for deadeye (stdio transport).

Same contract, different transport: the MCP tools map onto the CLI surface
(`review`, `doctor`, `schema`, `prompt`) and return the same shapes, so an
MCP client (an agent, a dashboard, a control script) reaches the gateway over
standard JSON-RPC instead of a subprocess. No second result format, no new
authority model.

The boundaries from the CLI do not weaken:

- `review` takes an explicit `allow_network` parameter and refuses the upload
  without it, exactly like `--allow-network`.
- Credentials still come only from the environment or `config.local.toml`;
  disclosure lines go to stderr; the redaction backstop still applies.
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
from pathlib import Path
from typing import Any

from . import __version__
from .cli import PROVIDERS, _resolve_provider, _resolve_timeout
from .errors import DeadeyeError
from .intent import load_intent_file, parse_intent_text
from .prompt import build_prompt
from .result import BASE_RUBRIC
from .review import run_review as run_review_core

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "deadeye"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "review",
        "description": "Submit a clip (frame directory or muxed video) plus its "
        "recorded intent to a vision model and return the advisory evidence "
        "envelope. Uploads the clip to a third party: refuses without "
        "allow_network=true. Every call is one new billable submission and "
        "never retries: resending this call after a lost response or a "
        "timeout submits the media again rather than replaying the first "
        "attempt.",
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


def _call_review(params: dict[str, Any]) -> dict[str, Any]:
    if not params.get("allow_network"):
        raise DeadeyeError(
            "review uploads the clip to a third party; pass allow_network=true to consent"
        )
    provider_name = _resolve_provider(params.get("provider"))
    # Same resolution and validation as the CLI flag: the tool argument, else
    # config's timeout_seconds, else the built-in default.
    timeout = _resolve_timeout(params.get("timeout_seconds"))
    output = Path(params["output"]) if params.get("output") else None
    return run_review_core(
        Path(params["clip"]),
        provider=PROVIDERS[provider_name](),
        intent_path=Path(params["intent"]) if params.get("intent") else None,
        intent_text=params.get("intent_text"),
        model=params.get("model"),
        allow_network=True,
        timeout_seconds=timeout,
        keep_raw_response=bool(params.get("keep_raw_response")),
        output=output,
        force=bool(params.get("force")),
    )


def _call_doctor(params: dict[str, Any]) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    for name, constructor in sorted(PROVIDERS.items()):
        provider = constructor()
        states.append(
            {
                "name": name,
                "endpoint_mode": provider.endpoint_mode,
                "state": "configured" if provider.is_configured() else "unavailable",
            }
        )
    return {"providers": states}


def _call_schema(params: dict[str, Any]) -> dict[str, Any]:
    from .intent import CAMERA_PATHS, INTENT_SCHEMA_VERSION
    from .result import RESULT_KEYS, RUBRIC_VERSION

    return {
        "intent": {
            "schema_version": INTENT_SCHEMA_VERSION,
            "required": ["purpose"],
            "fields": {
                "purpose": "string - what the clip is supposed to demonstrate",
                "subject": "string - the asset or behavior on screen",
                "camera_path": (f"string - one of {', '.join(CAMERA_PATHS)} or a free description"),
                "desired_qualities": (
                    "string - target proportions, silhouette, material read, timing"
                ),
                "avoid": "array of strings - clipping, popping, z-fighting, wrong scale, jitter",
                "references": "array of {path, purpose} comparison assets",
                "questions": "array of strings - concerns the reviewer must answer",
                "suite": "string - playtest suite id",
                "case": "string - playtest case id",
            },
        },
        "result": {
            "keys": list(RESULT_KEYS),
            "rubric_version": RUBRIC_VERSION,
            "dimensions": [item.key for item in BASE_RUBRIC],
        },
    }


def _call_prompt(params: dict[str, Any]) -> dict[str, Any]:
    from . import sampling

    if params.get("intent") and params.get("intent_text"):
        raise DeadeyeError("takes exactly one of intent or intent_text, never both")
    if params.get("intent"):
        intent, _ = load_intent_file(Path(params["intent"]))
    elif params.get("intent_text"):
        intent, _ = parse_intent_text(params["intent_text"])
    else:
        raise DeadeyeError("needs exactly one of intent or intent_text")
    if params.get("clip"):
        media = sampling.discover(Path(params["clip"]))
        if media.video is not None:
            summary = f"a single muxed video file ({media.video.name})"
            note = ""
        else:
            summary = f"{len(media.frames)} frame image(s) of the clip's {len(media.frames)} frames"
            note = "Frames arrive in the order listed; at_frame refers to that order."
    else:
        summary = "the submitted media (a muxed video or a sampled frame sequence)"
        note = ""
    return {
        "prompt": build_prompt(intent, BASE_RUBRIC, media_summary=summary, frame_timing_note=note)
    }


_CALLS: dict[str, Any] = {
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


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """The stdio loop: one JSON-RPC frame per line, responses on stdout."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_error(None, -32700, "Parse error")), file=stdout)
            stdout.flush()
            continue
        if not isinstance(frame, dict):
            print(json.dumps(_error(None, -32600, "Invalid Request")), file=stdout)
            stdout.flush()
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
            print(json.dumps(response), file=stdout)
            stdout.flush()
    return 0
