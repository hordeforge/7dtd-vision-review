"""The MCP server surface: JSON-RPC 2.0 over stdio, offline-testable.

`handle_frame` is pure, so the whole protocol — handshake, tool listing,
tool calls, spec error codes, and the review consent boundary — is pinned
without a socket.
"""

from __future__ import annotations

import json

from deadeye.mcp import PROTOCOL_VERSION, handle_frame


def _call(method: str, params: dict, request_id: int = 1) -> dict:
    response = handle_frame(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    assert response is not None
    return response


def test_initialize_pins_the_protocol_and_advertises_tools() -> None:
    response = _call("initialize", {})
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert response["result"]["capabilities"] == {"tools": {}}
    assert response["result"]["serverInfo"]["name"] == "deadeye"


def test_ping_and_tools_list() -> None:
    assert _call("ping", {})["result"] == {}
    tools = _call("tools/list", {})["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"review", "doctor", "schema", "prompt"}
    review = next(tool for tool in tools if tool["name"] == "review")
    assert "allow_network" in review["inputSchema"]["properties"]
    assert review["inputSchema"]["required"] == ["clip", "allow_network"]


def test_review_refuses_without_explicit_consent(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    response = _call("tools/call", {"name": "review", "arguments": {"clip": str(clip)}})
    assert response["result"]["isError"] is True
    assert "allow_network=true" in response["result"]["content"][0]["text"]


def test_review_with_a_fake_provider_returns_the_envelope(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "p"}), encoding="utf-8")
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {
                "clip": str(clip),
                "intent": str(intent),
                "provider": "fake",
                "allow_network": True,
            },
        },
    )
    assert response["result"].get("isError") is not True
    envelope = json.loads(response["result"]["content"][0]["text"])
    assert envelope["kind"] == "deadeye-review"
    assert envelope["provider"]["name"] == "fake"


def test_unknown_method_and_tool_get_spec_errors() -> None:
    error = _call("bogus", {})["error"]
    assert error["code"] == -32601
    response = _call("tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_notifications_are_ignored() -> None:
    assert handle_frame({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_schema_and_doctor_tools_return_json() -> None:
    schema = json.loads(
        _call("tools/call", {"name": "schema", "arguments": {}})["result"]["content"][0]["text"]
    )
    assert "summary" in schema["result"]["keys"]
    states = json.loads(
        _call("tools/call", {"name": "doctor", "arguments": {}})["result"]["content"][0]["text"]
    )
    assert {state["name"] for state in states["providers"]} == {"fake", "gemini", "nvidia"}


def test_prompt_tool_renders_the_injected_instruction(tmp_path) -> None:
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "show the turn"}), encoding="utf-8")
    payload = json.loads(
        _call("tools/call", {"name": "prompt", "arguments": {"intent": str(intent)}})["result"][
            "content"
        ][0]["text"]
    )
    assert "You are reviewing a game-asset candidate on screen." in payload["prompt"]
    assert "purpose: show the turn" in payload["prompt"]


def test_serve_round_trips_frames_over_pipes() -> None:
    import io

    from deadeye.mcp import serve

    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n'
        "not json\n"
    )
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["result"]["serverInfo"]["name"] == "deadeye"
    assert lines[1]["result"] == {}
    assert lines[2]["error"]["code"] == -32700
