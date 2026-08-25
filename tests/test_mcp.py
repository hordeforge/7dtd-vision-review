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


def test_review_announces_the_disclosure_lines_on_stderr(tmp_path, capsys) -> None:
    """The CLI's disclosure contract carries over the transport unchanged:
    what will leave the machine is announced on stderr before submission,
    while stdout stays protocol-only."""
    import io

    from deadeye import mcp

    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "p"}), encoding="utf-8")
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "review",
                    "arguments": {
                        "clip": str(clip),
                        "intent": str(intent),
                        "provider": "fake",
                        "allow_network": True,
                    },
                },
            }
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    captured = capsys.readouterr()
    err = captured.err
    assert "provider: fake" in err
    assert "submitting 1 file(s)" in err
    assert "warning: the media leaves this machine" in err
    # stdout carries exactly one protocol frame, no disclosure leakage.
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"].get("isError") is not True


def test_doctor_tool_returns_the_same_shape_as_the_cli() -> None:
    """Same contract, different transport: the doctor tool's states carry the
    same `detail` field `deadeye doctor --json` prints, and a keyless
    provider is never described as holding a key."""
    payload = json.loads(
        _call("tools/call", {"name": "doctor", "arguments": {}})["result"]["content"][0]["text"]
    )
    by_name = {state["name"]: state for state in payload["providers"]}
    assert set(by_name) == {"fake", "gemini", "nvidia"}
    assert all(state.get("detail") for state in payload["providers"])
    assert by_name["fake"]["detail"] == (
        "the fake provider needs no credentials; it exists for offline plumbing checks"
    )


def test_schema_tool_returns_exactly_what_deadeye_schema_prints() -> None:
    from deadeye.cli import schema_document

    payload = json.loads(
        _call("tools/call", {"name": "schema", "arguments": {}})["result"]["content"][0]["text"]
    )
    assert payload == schema_document()
    # The richer field documentation rides along, not just the key list.
    assert "issues" in payload["result"]


def test_review_honors_config_timeout_seconds(tmp_path, monkeypatch) -> None:
    """The MCP surface resolves the timeout exactly like the CLI flag."""
    from deadeye import config, mcp

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text("timeout_seconds = 77\n", encoding="utf-8")
    monkeypatch.setenv("DEADEYE_CONFIG_DIR", str(cfg))
    config.reset()
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")

    captured: dict = {}

    def fake_run(clip, **kwargs):
        captured.update(kwargs)
        return {"kind": "deadeye-review"}

    monkeypatch.setattr(mcp, "run_review_core", fake_run)
    try:
        response = _call(
            "tools/call",
            {
                "name": "review",
                "arguments": {"clip": str(clip), "provider": "fake", "allow_network": True},
            },
        )
        assert response["result"].get("isError") is not True
        assert captured["timeout_seconds"] == 77.0
    finally:
        config.reset()


def test_review_refuses_a_non_positive_timeout_instead_of_failing_late(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {
                "clip": str(clip),
                "provider": "fake",
                "allow_network": True,
                "timeout_seconds": 0,
            },
        },
    )
    assert response["result"]["isError"] is True
    assert "positive number of seconds" in response["result"]["content"][0]["text"]


def test_unknown_method_and_tool_get_spec_errors() -> None:
    error = _call("bogus", {})["error"]
    assert error["code"] == -32601
    response = _call("tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_a_missing_required_argument_names_the_tool_and_the_key(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    response = _call(
        "tools/call", {"name": "review", "arguments": {"allow_network": True, "clip": str(clip)}}
    )
    # intent/intent_text both absent: DeadeyeError carries the full message.
    assert response["result"]["isError"] is True
    assert "exactly one of --intent" in response["result"]["content"][0]["text"]

    from deadeye import mcp

    original = mcp._CALLS["review"]

    def missing_argument(params):
        raise KeyError("model_name")

    try:
        mcp._CALLS["review"] = missing_argument
        broken = _call("tools/call", {"name": "review", "arguments": {}})
        text = broken["result"]["content"][0]["text"]
        assert "'review'" in text and "KeyError" in text and "model_name" in text
    finally:
        mcp._CALLS["review"] = original


def test_an_internal_fault_answers_32603_and_keeps_the_session_alive(monkeypatch, capsys) -> None:
    """One faulty frame must not tear down the transport: the spec's
    internal-error code goes back and the next frame still gets served."""
    import io

    from deadeye import mcp

    def exploding_schema(params):
        raise RuntimeError("boom")

    monkeypatch.setitem(mcp._CALLS, "schema", exploding_schema)
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"schema","arguments":{}}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n'
    )
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32603
    assert lines[1]["result"] == {}
    assert "boom" in capsys.readouterr().err


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


def test_an_undecodable_frame_answers_parse_error_and_keeps_serving() -> None:
    """One invalid byte in a frame must not kill the transport inside the
    reader: it gets the same -32700 any malformed frame gets, and the next
    frame is still served."""
    import io

    from deadeye.mcp import serve

    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"p\xffng","params":{}}\n'
        b'{"jsonrpc":"2.0","id":3,"method":"ping","params":{}}\n'
    )
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["id"] == 3 and lines[2]["result"] == {}
