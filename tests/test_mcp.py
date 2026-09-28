"""The MCP server surface: JSON-RPC 2.0 over stdio, offline-testable.

`handle_frame` is pure, so the whole protocol — handshake, tool listing,
tool calls, spec error codes, and the review consent boundary — is pinned
without a socket.
"""

from __future__ import annotations

import errno
import io
import json
import unicodedata
from pathlib import Path

from deadeye import mcp
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


def test_every_tool_that_takes_an_intent_publishes_the_exactly_one_rule() -> None:
    """The core refuses a review naming neither intent route or both, so the
    published schema has to say it: `required` alone reads as "at least these"
    and a client building a `clip`-only call from it learns the rule only by
    collecting the refusal."""
    from deadeye.mcp import TOOLS

    for name in ("review", "prompt"):
        tool = next(tool for tool in TOOLS if tool["name"] == name)
        schema = tool["inputSchema"]
        assert schema["oneOf"] == [
            {"required": ["intent"], "not": {"required": ["intent_text"]}},
            {"required": ["intent_text"], "not": {"required": ["intent"]}},
        ], name


def test_review_refuses_without_explicit_consent(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    response = _call("tools/call", {"name": "review", "arguments": {"clip": str(clip)}})
    assert response["result"]["isError"] is True
    assert "allow_network=true" in response["result"]["content"][0]["text"]


def test_review_refuses_a_truthy_string_as_network_consent(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {"clip": str(clip), "allow_network": "true"},
        },
    )
    assert response["result"]["isError"] is True
    assert "JSON boolean" in response["result"]["content"][0]["text"]


def test_review_refuses_a_truthy_string_as_force(tmp_path) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "p"}), encoding="utf-8")
    output = tmp_path / "evidence.json"
    output.write_text("earlier evidence", encoding="utf-8")

    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {
                "clip": str(clip),
                "intent": str(intent),
                "provider": "fake",
                "allow_network": True,
                "output": str(output),
                "force": "false",
            },
        },
    )
    assert response["result"]["isError"] is True
    assert "force' must be a boolean" in response["result"]["content"][0]["text"]
    assert output.read_text(encoding="utf-8") == "earlier evidence"


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


def test_a_failed_evidence_write_returns_the_envelope_as_an_error_result(
    tmp_path, monkeypatch
) -> None:
    """The billed verdict rides the failure over MCP too: isError stays true
    (nothing was persisted), and the tool result text carries the full
    envelope so an agent recovers it without resubmitting the media."""
    from deadeye import evidence

    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "p"}), encoding="utf-8")
    output = tmp_path / "evidence.json"

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evidence, "_atomic_write", no_space)
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {
                "clip": str(clip),
                "intent": str(intent),
                "provider": "fake",
                "allow_network": True,
                "output": str(output),
            },
        },
    )
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["envelope"]["kind"] == "deadeye-review"
    assert payload["envelope"]["result"]["summary"]
    assert "cannot write evidence file" in payload["error"]


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


def test_doctor_tool_carries_the_effective_configuration_the_cli_prints() -> None:
    """The CLI's doctor is the gateway's only diagnosis, so a caller driving
    it over MCP must get the same facts: the tool result names the effective
    provider, model, and timeout the terminal prints beside the provider
    array, and never a credential value."""
    from deadeye.surface import config_diagnosis

    payload = json.loads(
        _call("tools/call", {"name": "doctor", "arguments": {}})["result"]["content"][0]["text"]
    )
    assert payload["config"] == config_diagnosis()
    assert payload["config"]["default_provider"] == {"value": "gemini"}
    assert payload["config"]["load_error"] is None
    assert set(payload["config"]) == {
        "sources",
        "load_error",
        "note",
        "example_path",
        "default_provider",
        "default_model",
        "timeout_seconds",
        "endpoint_problems",
    }


def test_schema_tool_returns_exactly_what_deadeye_schema_prints() -> None:
    from deadeye.surface import schema_document

    payload = json.loads(
        _call("tools/call", {"name": "schema", "arguments": {}})["result"]["content"][0]["text"]
    )
    assert payload == schema_document()
    # The richer field documentation rides along, not just the key list.
    assert "issues" in payload["result"]


def test_review_honors_config_timeout_seconds(tmp_path, monkeypatch, isolated_config) -> None:
    """The MCP surface resolves the timeout exactly like the CLI flag."""
    from deadeye import config, mcp

    (isolated_config / "config.toml").write_text("timeout_seconds = 77\n", encoding="utf-8")
    config.reset()
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")

    captured: dict = {}

    def fake_run(clip, **kwargs):
        captured.update(kwargs)
        return {"kind": "deadeye-review"}

    monkeypatch.setattr(mcp, "run_review_core", fake_run)
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {"clip": str(clip), "provider": "fake", "allow_network": True},
        },
    )
    assert response["result"].get("isError") is not True
    assert captured["timeout_seconds"] == 77.0


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


def test_review_refuses_an_unknown_provider_by_name(tmp_path) -> None:
    """`--provider` is bounded by argparse choices; the JSON-RPC argument is
    bounded by the same list, so a typo is a refusal rather than a KeyError
    dressed up as an internal fault."""
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    response = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": {
                "clip": str(clip),
                "provider": "genimi",
                "allow_network": True,
            },
        },
    )
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "'provider'" in text and "genimi" in text and "fake" in text
    assert "KeyError" not in text


def test_the_published_provider_enum_matches_the_registry() -> None:
    from deadeye.mcp import PROVIDERS, TOOLS

    review = next(tool for tool in TOOLS if tool["name"] == "review")
    assert review["inputSchema"]["properties"]["provider"]["enum"] == sorted(PROVIDERS)


def test_review_refuses_a_non_string_path_argument(tmp_path) -> None:
    """A number or a boolean would reach Path() as a TypeError naming no
    argument; the boundary names it instead."""
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    for name, value in (("clip", 7), ("intent", True), ("output", []), ("model", 3)):
        arguments = {"clip": str(clip), "provider": "fake", "allow_network": True}
        arguments[name] = value
        response = _call("tools/call", {"name": "review", "arguments": arguments})
        assert response["result"]["isError"] is True, name
        assert f"{name!r} must be a string" in response["result"]["content"][0]["text"], name
        assert "TypeError" not in response["result"]["content"][0]["text"], name


def test_review_names_a_missing_required_clip(tmp_path) -> None:
    response = _call(
        "tools/call",
        {"name": "review", "arguments": {"allow_network": True, "intent_text": "{}"}},
    )
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "'clip' is required" in text
    assert "KeyError" not in text


def test_prompt_refuses_a_non_string_argument(tmp_path) -> None:
    response = _call("tools/call", {"name": "prompt", "arguments": {"intent": 5}})
    assert response["result"]["isError"] is True
    assert "'intent' must be a string" in response["result"]["content"][0]["text"]


def test_a_refusal_carries_a_code_a_client_can_branch_on(tmp_path, monkeypatch) -> None:
    """The prose is for a person; the code is for the retry decision. A client
    holding an idempotency key has to know whether the submission was spent
    before it reuses the key, and `errors.py` already types the refusals for
    exactly that distinction."""

    from deadeye.providers import FakeProvider, ReviewResponse

    class UnusableProvider(FakeProvider):
        def review(self, request):
            self.requests.append(request)
            return ReviewResponse(raw_text="not json at all", usage=None, model_reported="fake")

    monkeypatch.setitem(mcp.PROVIDERS, "fake", UnusableProvider)

    arguments = _review_arguments(tmp_path)
    del arguments["intent"]
    usage = _call("tools/call", {"name": "review", "arguments": arguments})
    assert usage["result"]["isError"] is True
    assert usage["result"]["structuredContent"]["error"]["code"] == "usage"
    assert usage["result"]["content"][0]["text"].startswith("ERROR: ")

    spent = _call("tools/call", {"name": "review", "arguments": _review_arguments(tmp_path)})
    assert spent["result"]["structuredContent"]["error"]["code"] == "no_verdict"


def test_unknown_method_and_tool_get_spec_errors() -> None:
    error = _call("bogus", {})["error"]
    assert error["code"] == -32601
    response = _call("tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_every_published_tool_declares_its_required_arguments() -> None:
    """`required` is present on every tool, empty where there is nothing to
    require: a client reading the schemas should not have to know which of the
    two shapes `inputSchema` takes."""
    from deadeye.mcp import TOOLS

    for tool in TOOLS:
        assert "required" in tool["inputSchema"], tool["name"]


def test_a_frame_naming_another_protocol_version_is_an_invalid_request() -> None:
    """A `jsonrpc` member that is not "2.0" is a frame this server does not
    speak, which the spec classes as an invalid request rather than a method
    it does not know. A frame that omits the member entirely is served: the
    version is the one omission a lenient client makes without changing what
    the frame asks for."""
    wrong = mcp.handle_frame({"jsonrpc": "1.0", "id": 4, "method": "ping"})
    assert wrong == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {
            "code": -32600,
            "message": "Invalid Request: the jsonrpc member must be '2.0'",
        },
    }
    assert mcp.handle_frame({"id": 5, "method": "ping"})["result"] == {}


def test_a_batch_frame_names_batching_as_the_reason_it_is_refused() -> None:
    """This transport takes one request per line. A bare "Invalid Request" for
    an array leaves a client that batches reading it as a server fault, so the
    refusal says what to do instead."""
    out = io.StringIO()
    mcp.serve([json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]) + "\n"], out)
    answer = json.loads(out.getvalue())
    assert answer["error"]["code"] == -32600
    assert "batching is not supported" in answer["error"]["message"]


def test_an_argument_the_schema_does_not_declare_is_refused(tmp_path) -> None:
    """A misspelled argument used to be dropped, and the client learned about
    it from an unrelated refusal (`intetnt` reads as no intent route at all).
    The published properties are the allowlist, so the schema says so too."""
    from deadeye.mcp import TOOLS

    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in TOOLS)
    response = _call(
        "tools/call",
        {"name": "review", "arguments": {"allow_network": True, "intetnt": "intent.json"}},
    )
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "'intetnt'" in text and "intent_text" in text
    # No argument is submitted before the refusal: a wrong name must not reach
    # the provider path either.
    assert "allow_network=true" not in text


def test_a_falsy_non_object_params_member_is_invalid_params() -> None:
    """`params` and `arguments` are objects. A falsy non-object (`[]`, `""`,
    `0`) is the invalid params it is, not the empty object `or {}` would make
    it: reading it as absent would answer a malformed frame with a tool
    refusal or a successful call, and the client would never learn its frame
    was malformed."""
    for params in ([], "", 0, False):
        response = handle_frame({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params})
        assert response is not None
        assert response["error"]["code"] == -32602, params
        response = handle_frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "schema", "arguments": params},
            }
        )
        assert response is not None
        assert response["error"]["code"] == -32602, params


def test_an_omitted_or_null_params_member_is_still_served() -> None:
    """Absent and null are what the optional routes read as; a client that
    sends either must be served, not refused."""
    for params in ({}, None):
        response = handle_frame(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "schema", "arguments": params},
            }
        )
        assert response is not None and "result" in response, params


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


def test_a_null_id_is_a_request_not_a_notification() -> None:
    """JSON-RPC separates the two by the presence of the member: a frame
    carrying `"id": null` still gets an answer, and dropping it would leave
    the client waiting forever."""
    response = handle_frame({"jsonrpc": "2.0", "id": None, "method": "ping"})
    assert response == {"jsonrpc": "2.0", "id": None, "result": {}}


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


def test_only_json_whitespace_makes_a_line_blank() -> None:
    """A line is blank when it holds nothing but JSON's four whitespace
    characters. Every other content draws an answer: a line holding one
    control character that `str.strip()` would eat must still get the parse
    error, because a client waiting on that line is otherwise left in
    silence, which is the one outcome the transport promises never to do."""
    import io

    from deadeye.mcp import serve

    stdin = io.BytesIO(b"\x1c\n \t\r\n" + b"\x1d")
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["error"]["code"] for line in lines] == [-32700, -32700]


def test_crlf_delimited_frames_are_parsed_like_lf_frames() -> None:
    """A Windows MCP client may write JSON-RPC frames with CRLF; the trailing
    CR must not become part of the JSON, and the next frame stays aligned."""
    import io

    from deadeye.mcp import serve

    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\r\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\r\n'
    )
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["id"] == 1 and lines[0]["result"] == {}
    assert lines[1]["id"] == 2 and lines[1]["result"] == {}


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


def test_a_nested_beyond_the_limit_frame_is_a_parse_error_not_a_crash() -> None:
    """A frame nested beyond the interpreter limit makes json.loads raise
    RecursionError; that is malformed input, so it gets -32700 like any other
    bad frame instead of tearing down the long-lived server."""
    import io

    from deadeye.mcp import serve

    deep_frame = ("[" * 100000).encode() + b"\n"
    after = b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}\n'
    stdout = io.StringIO()
    assert serve(io.BytesIO(deep_frame + after), stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["id"] == 7 and lines[1]["result"] == {}


def test_an_oversized_frame_is_a_parse_error_and_keeps_serving(monkeypatch) -> None:
    """A JSON-RPC line with no bound would retain whatever a client writes
    until the next newline. An oversized frame must be discarded through that
    newline so the next request is still aligned, and answered as parse error
    rather than tearing the session down."""
    import io

    from deadeye import mcp

    monkeypatch.setattr(mcp, "_MAX_FRAME_BYTES", 64)
    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n'
        + b"x" * 200
        + b"\n"
        + b'{"jsonrpc":"2.0","id":3,"method":"ping","params":{}}\n'
    )
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["id"] == 3 and lines[2]["result"] == {}


class _UnusableGeminiAnswer(io.BytesIO):
    """A urlopen stand-in carrying an answer the adapter cannot use.

    The request reached the provider and it answered 200 with a body holding
    no candidate, so a review may already have run and billed on the far
    side. The stub is the real adapter's own input, not a hand-raised fault,
    so the test pins the whole chain from the response to the ledger.
    """

    def __init__(self) -> None:
        super().__init__(b'{"candidates": []}')

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_a_line_of_non_json_whitespace_is_a_parse_error_not_silence() -> None:
    """A line holding only a character JSON does not call whitespace is a
    malformed frame, and a client that wrote one waits for an answer.

    `str.strip()` removes every Unicode whitespace character, so a line of
    U+001C (or U+0085, or U+2028) used to read as blank and was dropped
    without a word, hanging the client on a frame it had every right to send.
    Only space, tab, CR and LF make a line blank.
    """
    import io

    from deadeye.mcp import serve

    stdin = io.BytesIO(
        b"\x1c\n"
        b"\xc2\x85\n"  # U+0085 NEL
        b"\xe2\x80\xa8\n"  # U+2028 LINE SEPARATOR
        b" \t\r\n"  # JSON whitespace alone: the one silent line
        + b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n'
    )
    stdout = io.StringIO()
    assert serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["error"]["code"] for line in lines[:3]] == [-32700, -32700, -32700]
    assert lines[3]["id"] == 1 and lines[3]["result"] == {}


def _review_arguments(tmp_path, **extra) -> dict:
    """A complete, consented `review` call against the offline fake provider."""
    clip = tmp_path / "clip"
    clip.mkdir(exist_ok=True)
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps({"purpose": "p"}), encoding="utf-8")
    return {
        "clip": str(clip),
        "intent": str(intent),
        "provider": "fake",
        "allow_network": True,
        **extra,
    }


def test_a_repeated_review_call_with_the_same_key_submits_once(tmp_path) -> None:
    """A client that retries its own call (lost response, timeout, replay)
    hands back the identical call. With an idempotency key the retry returns
    the first envelope instead of paying for a second submission, so the two
    results are the same document, not two verdicts."""
    from deadeye import mcp

    submissions = 0
    original = mcp.run_review_core

    def counted(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return original(*args, **kwargs)

    mcp.run_review_core = counted
    try:
        arguments = _review_arguments(tmp_path, idempotency_key="job-42-thing")
        first = _call("tools/call", {"name": "review", "arguments": arguments})
        second = _call("tools/call", {"name": "review", "arguments": arguments})
    finally:
        mcp.run_review_core = original

    assert submissions == 1, "the retry must not reach the provider"
    assert first["result"] == second["result"]


def test_a_key_retry_in_the_other_normalization_form_still_submits_once(tmp_path) -> None:
    """The key names one logical operation, and the ledger lookup is an
    identity comparison on it. A key that reaches the client decomposed (macOS
    composes nothing it receives, and a paste carries whatever the source had)
    spells the same name with combining marks where the composed form has
    precomposed characters. As two ledger entries the retry answered a
    different question and billed the media twice."""
    from deadeye import mcp

    submissions = 0
    original = mcp.run_review_core

    def counted(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return original(*args, **kwargs)

    mcp.run_review_core = counted
    try:
        composed = unicodedata.normalize("NFC", "café-job")
        decomposed = unicodedata.normalize("NFD", "café-job")
        assert composed != decomposed
        first = _call(
            "tools/call",
            {"name": "review", "arguments": _review_arguments(tmp_path, idempotency_key=composed)},
        )
        second = _call(
            "tools/call",
            {
                "name": "review",
                "arguments": _review_arguments(tmp_path, idempotency_key=decomposed),
            },
        )
    finally:
        mcp.run_review_core = original

    assert submissions == 1, "the decomposed retry must not reach the provider"
    assert first["result"] == second["result"]


def test_a_repeated_call_without_a_key_is_still_a_second_submission(tmp_path) -> None:
    """The key is opt-in: the documented default (a duplicate call bills
    again) is untouched, because a client that named no operation cannot be
    told which of its calls was the duplicate."""
    from deadeye import mcp

    submissions = 0
    original = mcp.run_review_core

    def counted(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return original(*args, **kwargs)

    mcp.run_review_core = counted
    try:
        first = _call("tools/call", {"name": "review", "arguments": _review_arguments(tmp_path)})
        second = _call("tools/call", {"name": "review", "arguments": _review_arguments(tmp_path)})
    finally:
        mcp.run_review_core = original

    assert submissions == 2
    first_envelope = json.loads(first["result"]["content"][0]["text"])
    second_envelope = json.loads(second["result"]["content"][0]["text"])
    assert first_envelope["review_id"] != second_envelope["review_id"]


def test_one_idempotency_key_cannot_serve_two_different_calls(tmp_path) -> None:
    """Reusing a key for different arguments would either return another
    operation's verdict or bill twice under a name already spent. It is
    refused, and the first call's verdict stays under its own key."""
    first = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": _review_arguments(tmp_path, idempotency_key="job-42", model="a"),
        },
    )
    clash = _call(
        "tools/call",
        {
            "name": "review",
            "arguments": _review_arguments(tmp_path, idempotency_key="job-42", model="b"),
        },
    )
    assert first["result"].get("isError") is not True
    assert clash["result"]["isError"] is True
    assert "different arguments" in clash["result"]["content"][0]["text"]


def test_a_refused_call_never_occupies_its_idempotency_key(tmp_path) -> None:
    """A refusal that happens before the submission is safe to retry, so it
    must not be frozen into the ledger: the same key and a corrected call must
    still run afterwards. Both preflight refusals count: a missing clip, and
    an occupied evidence path."""
    from deadeye import mcp

    arguments = _review_arguments(tmp_path, idempotency_key="job-43")
    good_clip = arguments["clip"]

    arguments["clip"] = str(tmp_path / "no-such-clip")
    refused = _call("tools/call", {"name": "review", "arguments": arguments})
    assert refused["result"]["isError"] is True
    assert not mcp._COMPLETED

    arguments["clip"] = good_clip
    occupied = tmp_path / "taken.json"
    occupied.write_text("earlier evidence", encoding="utf-8")
    arguments["output"] = str(occupied)
    refused = _call("tools/call", {"name": "review", "arguments": arguments})
    assert refused["result"]["isError"] is True
    assert not mcp._COMPLETED, "an occupied path is refused before the submission"

    arguments.pop("output")
    recovered = _call("tools/call", {"name": "review", "arguments": arguments})
    assert recovered["result"].get("isError") is not True


def test_a_billed_review_whose_evidence_write_failed_still_occupies_its_key(
    tmp_path, monkeypatch
) -> None:
    """The ledger exists so a retry never bills the same media twice. A
    verdict that was returned and then failed to reach disk was still billed,
    so the key holds that answer and a retry replays it, fault and all,
    instead of submitting the clip again."""
    from deadeye import evidence, mcp

    submissions = 0
    original = mcp.run_review_core

    def counted(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return original(*args, **kwargs)

    occupied = tmp_path / "taken.json"
    occupied.write_text("earlier evidence", encoding="utf-8")

    # A full filesystem after the verdict returned: the destination directory
    # is writable at preflight and stops being at write time, so the fault
    # arrives after the submission, which is the billed case the ledger exists
    # for.
    def out_of_space(path: Path, payload: bytes, *, force: bool) -> None:
        raise OSError(errno.ENOSPC, "No space left on device", str(path))

    monkeypatch.setattr(evidence, "_atomic_write", out_of_space)
    arguments = _review_arguments(
        tmp_path, idempotency_key="job-44", output=str(tmp_path / "evidence.json")
    )
    mcp.run_review_core = counted
    try:
        first = _call("tools/call", {"name": "review", "arguments": arguments})
        second = _call("tools/call", {"name": "review", "arguments": arguments})
    finally:
        mcp.run_review_core = original

    assert submissions == 1, "the retry must not reach the provider a second time"
    assert first["result"]["isError"] is True
    assert first["result"] == second["result"]
    assert "envelope" in first["result"]["content"][0]["text"]


def test_a_billed_review_the_provider_answered_nothing_usable_for_occupies_its_key(
    tmp_path, monkeypatch
) -> None:
    """A submission that was sent and billed, and came back unusable, spent
    its key exactly as a verdict that failed to reach disk did.

    Nothing reaches the ledger for a local refusal, which is right: a request
    the provider never saw is safe to resend. This is the opposite case. The
    media crossed the network, the provider answered text the result schema
    rejects, and the attempt is billed. A client that retries the same call
    under the same key is retrying into a second charge for the same bytes,
    and would most likely get the same unusable answer, so the repeat replays
    the first refusal instead of submitting again.
    """
    from deadeye import mcp
    from deadeye.providers import ReviewResponse
    from deadeye.providers.fake import FakeProvider

    class UnusableProvider(FakeProvider):
        def review(self, request):
            self.requests.append(request)
            return ReviewResponse(raw_text="not json at all", usage=None, model_reported="fake")

    provider = UnusableProvider()
    monkeypatch.setitem(mcp.PROVIDERS, "fake", lambda: provider)
    arguments = _review_arguments(tmp_path, idempotency_key="job-45")

    first = _call("tools/call", {"name": "review", "arguments": arguments})
    second = _call("tools/call", {"name": "review", "arguments": arguments})

    assert len(provider.requests) == 1, "the retry must not reach the provider a second time"
    assert first["result"]["isError"] is True
    assert first["result"] == second["result"]
    assert "structural validation" in first["result"]["content"][0]["text"]


def test_a_billed_review_the_adapter_found_unusable_occupies_its_key(tmp_path, monkeypatch) -> None:
    """A refusal the adapter itself raises, after the provider answered, spends
    the key like any other unusable answer.

    The adapters' own post-submission refusals (no candidate, empty text, a
    generation cut short) are `NoVerdictError` for this reason. A plain
    `DeadeyeError` reads to the ledger as a request the provider never saw, and
    the client's retry then pays a second time for the same bytes.
    """
    from deadeye import mcp
    from deadeye.errors import NoVerdictError
    from deadeye.providers.fake import FakeProvider

    class UnusableProvider(FakeProvider):
        def review(self, request):
            self.requests.append(request)
            raise NoVerdictError("provider 'fake' returned no candidate; no verdict was produced")

    provider = UnusableProvider()
    monkeypatch.setitem(mcp.PROVIDERS, "fake", lambda: provider)
    arguments = _review_arguments(tmp_path, idempotency_key="job-47")

    first = _call("tools/call", {"name": "review", "arguments": arguments})
    second = _call("tools/call", {"name": "review", "arguments": arguments})

    assert len(provider.requests) == 1, "the retry must not reach the provider a second time"
    assert first["result"]["isError"] is True
    assert first["result"] == second["result"]


def test_a_billed_review_with_no_usable_verdict_leaves_no_envelope_to_replay(
    tmp_path, monkeypatch
) -> None:
    """The entry for such a call keeps the refusal and no envelope, so the
    replay raises the fault rather than answering with an empty document."""
    from deadeye import mcp
    from deadeye.providers import ReviewResponse
    from deadeye.providers.fake import FakeProvider

    class UnusableProvider(FakeProvider):
        def review(self, request):
            self.requests.append(request)
            return ReviewResponse(raw_text="{}", usage=None, model_reported="fake")

    monkeypatch.setitem(mcp.PROVIDERS, "fake", UnusableProvider)
    _call(
        "tools/call",
        {"name": "review", "arguments": _review_arguments(tmp_path, idempotency_key="job-46")},
    )

    entry = mcp._COMPLETED["job-46"]
    assert entry.envelope is None
    assert entry.fault is not None


def test_a_refusal_on_an_answer_the_provider_already_gave_spends_its_key(
    tmp_path, monkeypatch, http_opener
) -> None:
    """The exception type is how a deduplicating transport knows a call is spent.

    A hosted provider can answer 2xx with an envelope no verdict can be read
    out of: an empty candidate list, a finish reason that cut the generation
    short, a body that is not JSON. The media crossed the network and the
    model ran, so the attempt may already be billed, and the ledger can only
    tell a spent key from a free one by the exception type. A refusal an
    adapter raised as an ordinary `DeadeyeError` would leave the key free, and
    a client retrying a call whose answer the adapter could not use would pay
    a second time for the same bytes, which is exactly what naming the key
    promised to prevent. This drives the real Gemini adapter over a stubbed
    transport, so the rule is pinned where it is decided rather than mocked
    into being.
    """
    from deadeye import mcp

    submissions = 0

    def answered(request, timeout):
        nonlocal submissions
        submissions += 1
        return _UnusableGeminiAnswer()

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    http_opener(answered)
    arguments = _review_arguments(tmp_path, provider="gemini", idempotency_key="job-47")

    first = _call("tools/call", {"name": "review", "arguments": arguments})
    second = _call("tools/call", {"name": "review", "arguments": arguments})

    assert submissions == 1, "the retry must not reach the provider a second time"
    assert first["result"]["isError"] is True
    assert first["result"] == second["result"]
    assert "no candidate" in first["result"]["content"][0]["text"]
    assert "not a retry of this one" in first["result"]["content"][0]["text"]
    assert mcp._COMPLETED["job-47"].envelope is None
    assert mcp._COMPLETED["job-47"].fault is not None


def test_the_idempotency_ledger_is_bounded(tmp_path, monkeypatch) -> None:
    """A long-lived server must not accumulate one entry per key a client
    ever names: the oldest is evicted at the bound, and a key past the bound
    submits again rather than being answered from a forgotten entry."""
    from deadeye import mcp

    monkeypatch.setattr(mcp, "_IDEMPOTENCY_LEDGER_ENTRIES", 2)
    for index in range(3):
        response = _call(
            "tools/call",
            {
                "name": "review",
                "arguments": _review_arguments(tmp_path, idempotency_key=f"job-{index}"),
            },
        )
        assert response["result"].get("isError") is not True
    assert list(mcp._COMPLETED) == ["job-1", "job-2"]


def test_the_idempotency_ledger_is_bounded_by_retained_bytes_too(tmp_path, monkeypatch) -> None:
    """The entry count is not the cost. One entry carries a whole envelope,
    and `keep_raw_response` puts a redacted provider payload in it, which the
    HTTP reader bounds at 8 MiB; a few dozen fat keys would pin a gigabyte in
    a server that is supposed to idle between reviews. A byte budget evicts
    the oldest, and the entry just answered is always kept so the key a
    client is most likely to retry still replays."""
    from deadeye import mcp

    monkeypatch.setattr(mcp, "_IDEMPOTENCY_LEDGER_MAX_BYTES", 1)
    for index in range(3):
        response = _call(
            "tools/call",
            {
                "name": "review",
                "arguments": _review_arguments(tmp_path, idempotency_key=f"job-{index}"),
            },
        )
        assert response["result"].get("isError") is not True
    assert list(mcp._COMPLETED) == ["job-2"]


def test_an_unusable_idempotency_key_is_refused_before_any_submission(tmp_path) -> None:
    """A key that is not a usable name (empty, not a string, absurdly long)
    is a client bug, and it is caught before anything is submitted."""
    from deadeye import mcp

    for bad in ["", "   ", 7, None, "k" * (mcp._MAX_IDEMPOTENCY_KEY_CHARS + 1)]:
        response = _call(
            "tools/call",
            {
                "name": "review",
                "arguments": _review_arguments(tmp_path, idempotency_key=bad),
            },
        )
        assert response["result"]["isError"] is True, bad
        assert "idempotency_key" in response["result"]["content"][0]["text"]
    assert not mcp._COMPLETED


def test_the_frame_cap_counts_bytes_on_a_text_transport(monkeypatch) -> None:
    """`_MAX_FRAME_BYTES` is named in bytes and the stdio transport is bytes,
    so a text frame reaching the same cap through a test double or an
    already-split iterable must be measured in bytes too. Counting code
    points there admitted a frame of four-byte characters at four times the
    intended size."""
    import io

    from deadeye import mcp

    monkeypatch.setattr(mcp, "_MAX_FRAME_BYTES", 64)
    # 22 characters, 66 UTF-8 bytes: over the cap as bytes, under it as
    # characters. The next frame must still be served.
    oversized_text = "\U0001f600" * 22
    stdin = io.StringIO(
        oversized_text + "\n" + '{"jsonrpc":"2.0","id":3,"method":"ping","params":{}}\n'
    )
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["id"] == 3 and lines[1]["result"] == {}
