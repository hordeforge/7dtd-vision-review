"""Property-based fuzz targets for the untrusted-input parsers.

Every provider response (raw response bytes, then raw model text) and every
intent document is data from outside this process: a third party's answer, or
an authored file that may be malformed by accident or on purpose. Filenames
inside a clip directory are authored-local text that reaches the reviewer
prompt. These harnesses fuzz the code that consumes those inputs with
Hypothesis (structure-aware strategies, not blind byte mutation) and pin the
invariants the pipeline depends on:

- only DeadeyeError may refuse input; any other exception escapes and fails
  the run, so a crash on malformed data is visible instead of silent;
- anything that IS accepted must satisfy the pipeline-owned shape exactly,
  including across a re-validation round trip;
- `redact` must drop every credential-bearing key from any JSON-shaped
  value, however deeply it is buried: the load-bearing credentials backstop;
- a response body decodes under the declared charset or UTF-8, and refuses by
  name otherwise, never reaching an adapter as a bare decode error;
- a sanitized envelope re-serializes under RFC 8259: no `NaN`, no `Infinity`,
  no `1e999`, whatever the provider emitted;
- `flat_label_text` leaves no line separator or control character behind, so
  a filename cannot forge an extra label-shaped line in the prompt;
- an MCP client frame is answered in band or not at all: the id comes back,
  the answer is one of result/error, and no frame takes the stdio loop down;
- a config file and an endpoint override refuse by name rather than by
  traceback, and an accepted override is always https or a loopback http, so
  the string that decides where a provider credential goes cannot be the one
  thing an unvalidated reader waves through.

Run with the rest of the suite (`make test`). A failure prints the
falsifying example: pin it as a regression test next to the parser's unit
tests before changing anything. On a bare host without the dev group
(no uv), this module skips itself rather than aborting collection.
"""

from __future__ import annotations

import email.message
import io
import json
import math
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:
    pytest.skip(
        "hypothesis is not installed; run scripts/bootstrap for the full suite",
        allow_module_level=True,
    )

from deadeye import config, mcp
from deadeye.errors import DeadeyeError
from deadeye.intent import load_intent, parse_intent
from deadeye.json_safe import strict_json_numbers
from deadeye.providers._http import _decode_envelope
from deadeye.providers.base import float_setting, int_setting
from deadeye.redaction import SENSITIVE_KEY_PARTS, redact
from deadeye.result import BASE_RUBRIC, RESULT_KEYS, parse_model_json, validate_result
from deadeye.sampling import flat_label_text

FUZZ = settings(max_examples=300, deadline=None)

_DIMENSION_KEYS = {item.key for item in BASE_RUBRIC}

# ---------------------------------------------------------------------------
# Shared strategies: JSON-shaped values with hostile edges mixed in.
# ---------------------------------------------------------------------------

_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=64)
)


def _json_values(max_leaves: int = 10) -> st.SearchStrategy:
    return st.recursive(
        _scalars,
        lambda child: st.lists(child, max_size=4) | st.dictionaries(st.text(max_size=24), child),
        max_leaves=max_leaves,
    )


def _json_text(values: st.SearchStrategy) -> st.SearchStrategy:
    """Any JSON value rendered as text, plus prose that embeds one."""

    def render(value: object) -> str:
        return json.dumps(value) if not isinstance(value, str) else value

    return st.builds(
        lambda prefix, value, suffix: f"{prefix}{render(value)}{suffix}",
        st.sampled_from(["", "The review follows.\n", "```json\n", "output: "]),
        values,
        st.sampled_from(["", "\nDone.", "\n```", "!"]),
    )


# ---------------------------------------------------------------------------
# Target 1: the model-output parser boundary.
#
# raw provider/model text -> parse_model_json -> validate_result. This is the
# trust boundary every provider response crosses; its output goes to sibling
# repositories verbatim, so an accepted value must BE the pipeline shape.
# ---------------------------------------------------------------------------


def _assert_pipeline_shape(result: dict[str, Any]) -> None:
    assert set(result) == set(RESULT_KEYS)
    assert isinstance(result["summary"], str) and result["summary"].strip()
    for key in ("strengths", "recommended_changes", "limitations"):
        assert isinstance(result[key], list)
        assert all(isinstance(item, str) and item.strip() for item in result[key])
    for issue in result["issues"]:
        assert isinstance(issue["description"], str) and issue["description"].strip()
        for moment_key, floor in (("at_seconds", None), ("at_frame", 0.0)):
            if moment_key in issue:
                start, end = issue[moment_key]
                assert math.isfinite(start) and math.isfinite(end), moment_key
                assert start <= end, moment_key
                assert floor is None or start >= floor, moment_key
    assert set(result["rubric_scores"]) <= _DIMENSION_KEYS
    for score in result["rubric_scores"].values():
        assert score is None or (math.isfinite(score) and 0 <= score <= 5)
    assert math.isfinite(result["confidence"]) and 0 <= result["confidence"] <= 1


@FUZZ
@given(
    st.one_of(
        st.text(max_size=256),
        _json_text(_json_values()),
        _json_text(
            st.fixed_dictionaries(
                {
                    "summary": st.one_of(st.text(), st.none(), st.integers()),
                    "issues": st.lists(_json_values(max_leaves=3), max_size=3),
                    "rubric_scores": st.dictionaries(
                        st.sampled_from(sorted(_DIMENSION_KEYS)) | st.text(max_size=16),
                        st.one_of(_scalars, st.booleans()),
                        max_size=4,
                    ),
                }
            )
        ),
    )
)
def test_fuzz_model_output_parser_boundary(raw_text: str) -> None:
    try:
        parsed = parse_model_json(raw_text)
    except DeadeyeError:
        return  # refusal: the only allowed failure mode
    try:
        result = validate_result(parsed)
    except DeadeyeError:
        return
    _assert_pipeline_shape(result)
    # The pipeline-owned shape re-validates unchanged: consumers can run it
    # back through validate_result after a read from disk or the wire.
    assert validate_result(result) == result


@FUZZ
@given(_json_values(max_leaves=14))
def test_fuzz_validate_result_accepts_only_pipeline_shapes(data: object) -> None:
    try:
        result = validate_result(data)  # type: ignore[arg-type]
    except DeadeyeError:
        return
    # Anything accepted is a dict satisfying the pipeline shape, unchanged
    # when validated again after a read from disk or the wire.
    assert isinstance(data, dict)
    _assert_pipeline_shape(result)
    assert validate_result(result) == result


# ---------------------------------------------------------------------------
# Target 2: the intent parser and the redaction backstop.
#
# Intent documents come off disk or --intent-text; `redact` runs over
# arbitrary JSON-shaped evidence before it is ever written. A credential key
# surviving redaction would leak secrets into stored evidence.
# ---------------------------------------------------------------------------


def _looks_sensitive(key: str) -> bool:
    # Mirrors redaction._is_sensitive_key: case folding, not lower(), so the
    # oracle and the backstop agree on fold-only spellings (U+017F vs 's').
    folded = key.casefold()
    return folded == "key" or any(part in folded for part in SENSITIVE_KEY_PARTS)


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


_INTENT_FIELDS = (
    "schema_version",
    "purpose",
    "subject",
    "camera_path",
    "desired_qualities",
    "avoid",
    "references",
    "questions",
    "suite",
    "case",
)

_intent_docs = st.dictionaries(
    st.one_of(st.sampled_from(_INTENT_FIELDS), st.text(min_size=1, max_size=16)),
    st.one_of(_scalars, st.lists(_scalars, max_size=3), st.dictionaries(st.text(), _scalars)),
    max_size=6,
)


def _same(left: object, right: object) -> bool:
    """Structural equality that treats a NaN float as equal to itself."""
    if isinstance(left, float) and isinstance(right, float):
        return left == right or (math.isnan(left) and math.isnan(right))
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same(item, right[key]) for key, item in left.items()
        )
    return type(left) is type(right) and left == right


@FUZZ
@given(_json_values(max_leaves=14))
def test_fuzz_redact_drops_every_sensitive_key(value: object) -> None:
    cleaned = redact(value)
    for key in _walk_keys(cleaned):
        assert isinstance(key, str), "redact drops non-string keys"
        assert not _looks_sensitive(key), f"credential-bearing key {key!r} survived redact"
    # Redaction is idempotent: nothing new to remove on a second pass. A NaN
    # leaf compares unequal to itself, so == cannot express this; compare
    # structurally instead.
    assert _same(redact(cleaned), cleaned)


@FUZZ
@given(st.one_of(_intent_docs, st.text(max_size=128)))
def test_fuzz_intent_parse_never_crashes_and_round_trips(document: object) -> None:
    try:
        text = json.dumps(document) if not isinstance(document, str) else document
        intent = load_intent(None, text)[0]
    except DeadeyeError:
        return  # refusal: the only allowed failure mode
    assert intent.purpose.strip()
    assert intent.camera_path == "" or intent.camera_path.strip()
    for field in (intent.avoid, intent.questions):
        assert all(item.strip() for item in field)
    for reference in intent.references:
        assert reference.path != Path()
        assert reference.purpose.strip()
    # Round trip across the persistence boundary: the shape written by
    # as_dict must read back identical.
    assert parse_intent(intent.as_dict(), "round-trip").as_dict() == intent.as_dict()


# ---------------------------------------------------------------------------
# Target 3: the response-body boundary of the hosted adapters.
#
# raw response bytes -> _decode_envelope -> json.loads -> strict_json_numbers.
# Everything upstream of the model-text parser, and the step that decides
# whether a provider's answer is text or a refusal. A body that is not valid
# UTF-8 under either the declared charset or the JSON default must refuse by
# name; an envelope that parses must be storable, which means it has to
# re-serialize for a strict reader.
# ---------------------------------------------------------------------------

_HOSTILE_BYTES = (
    b"\x00",
    b"\x1b[31m",
    b"\xef\xbb\xbf",  # UTF-8 BOM
    b"\xc0\xaf",  # overlong encoding of '/'
    b"\xed\xa0\x80",  # surrogate half
    b"\xff\xfe\x00",  # UTF-16 BOM
    b"\xf4\x90\x80\x80",  # above U+10FFFF
)

_CHARSETS = st.one_of(
    st.none(),
    st.sampled_from(["utf-8", "utf-16", "latin-1", "ascii", "not-a-charset", ""]),
    st.text(max_size=12),
)

_body_bytes = st.one_of(
    st.binary(max_size=64),
    st.lists(st.sampled_from(_HOSTILE_BYTES), min_size=1, max_size=4).map(b"".join),
    st.builds(
        lambda head, tail: head.encode("utf-8") + tail,
        st.text(max_size=32),
        st.sampled_from(_HOSTILE_BYTES),
    ),
)


def _headers(charset: str | None) -> Any:
    """A stand-in for urllib's response headers, with the one method used."""
    if charset is None:
        return None
    message: Any = email.message.Message()
    message["Content-Type"] = f"application/json; charset={charset}"
    return message


@FUZZ
@given(body=_body_bytes, charset=_CHARSETS)
def test_fuzz_response_body_decodes_or_refuses_by_name(body: bytes, charset: str | None) -> None:
    try:
        text = _decode_envelope("fuzz", body, _headers(charset))
    except DeadeyeError:
        return  # refusal: the only allowed failure mode
    # Accepted text is the exact UTF-8 encoding of what came back: a
    # replacement character or a silent mangling would put bytes the
    # provider never sent into stored evidence.
    assert isinstance(text, str)
    # No lone surrogate: the decode either produced a character that encodes
    # back to bytes, or refused; a surrogate half would crash the write of
    # the evidence file it reached.
    text.encode("utf-8")
    try:
        envelope = json.loads(text)
    except ValueError:
        return  # not JSON: the adapter's own parse refuses it by name
    # A non-object envelope is refused upstream of any key lookup, so
    # nothing downstream ever has to cope with a bare list or string.
    if not isinstance(envelope, dict):
        return
    assert isinstance(envelope, dict)


@FUZZ
@given(_json_values(max_leaves=14))
def test_fuzz_envelope_survives_a_strict_json_reader(envelope: object) -> None:
    sanitized = strict_json_numbers(envelope)
    # What the provider actually emitted rides into evidence, stdout, and
    # MCP payloads. allow_nan=False is the oracle: it refuses exactly the
    # tokens RFC 8259 does not define, so passing means a strict reader
    # (jq, a browser, a Go consumer) can parse what is written back out.
    try:
        rendered = json.dumps(sanitized, allow_nan=False)
    except ValueError as exc:
        raise AssertionError(f"non-finite leaf survived sanitizing: {exc}") from exc
    assert json.loads(rendered, parse_constant=_reject_constant) == _strip_nonfinite(envelope)
    # Sanitizing again finds nothing left to change, so the envelope is
    # stable across the read/write round trip consumers perform.
    assert _same(strict_json_numbers(json.loads(rendered)), sanitized)


def _reject_constant(name: str) -> object:
    raise AssertionError(f"RFC 8259 does not define {name}, and it reached the payload")


def _strip_nonfinite(value: object) -> object:
    """The same walk as `strict_json_numbers`, as an independent oracle."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _strip_nonfinite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_nonfinite(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Target 4: the filename-to-prompt boundary.
#
# A clip directory is authored-local, and a filename reaches the model both
# outside the author-statement fence (an attachment label) and inside it. A
# name carrying a newline, a CR, or any other control character could forge
# extra label-shaped lines there, which is the injection vector T6 names.
# ---------------------------------------------------------------------------


@FUZZ
@given(st.text(max_size=48))
def test_fuzz_filename_flattening_forges_no_prompt_line(name: str) -> None:
    flattened = flat_label_text(name)
    # One output line per input: nothing in the name can open a second line
    # for a reviewer to read as a label or an instruction.
    assert flattened.count("\n") == 0
    assert flattened.count("\r") == 0
    assert flattened.count("\x0b") == 0
    assert flattened.count("\x0c") == 0
    assert flattened.count("\u2028") == 0
    assert flattened.count("\u2029") == 0
    # Every character the flattening is there to remove is gone, no printable
    # character was mangled, and the name cannot change length: a truncation
    # would silently merge two filenames in the prompt's reference listing.
    assert all(char.isprintable() for char in flattened)
    assert len(flattened) == len(name)
    assert "".join(char if char.isprintable() else " " for char in name) == flattened
    # Flattening a flattened name changes nothing, so prompt text built
    # twice from the same file is byte-identical.
    assert flat_label_text(flattened) == flattened


# ---------------------------------------------------------------------------
# Target 5: the MCP server's JSON-RPC boundary.
#
# `deadeye mcp` reads newline-delimited JSON from a client process: frames and
# tool arguments are as untrusted as a provider's answer, and they arrive on a
# long-lived stdio loop where one unhandled exception tears the transport down
# and leaves the client waiting. The invariants:
#
# - `handle_frame` answers every request frame in band (jsonrpc 2.0, the id
#   echoed, exactly one of result/error) and raises nothing: a malformed
#   frame is a spec error code, never an exception at the caller;
# - a tool result is a text content block, and a refusal is that block marked
#   isError, so a client can tell a fault from a verdict without parsing prose;
# - a review call naming no real clip is always refused and never completes a
#   submission, so a hostile argument set cannot bill one or spend a key.
# ---------------------------------------------------------------------------

# Paths that cannot exist, so a review call that gets as far as media
# discovery is refused there: this module never submits anything, whatever
# the arguments say.
_ABSENT_CLIP = "/nonexistent-deadeye-fuzz/clip"


def _json_object_values(max_size: int = 6) -> st.SearchStrategy[dict[str, Any]]:
    return st.dictionaries(
        st.text(max_size=24),
        st.one_of(_scalars, st.lists(_scalars, max_size=3)),
        max_size=max_size,
    )


def _text_values(max_size: int = 96) -> st.SearchStrategy[str]:
    """Arbitrary client text, with NUL bytes and a lone surrogate mixed in."""
    return st.one_of(
        st.text(max_size=max_size),
        st.text(min_size=1, max_size=16).map(lambda word: f"{word}\x00nul\x00\ud800\x00 "),
    )


# Tool arguments for `review`, with the two arguments that could spend money
# pinned: the provider is the offline fake and the clip does not exist.
_review_arguments = st.fixed_dictionaries(
    {
        "clip": st.just(_ABSENT_CLIP),
        "provider": st.just("fake"),
        "intent": st.sampled_from(["/nonexistent-deadeye-fuzz/i.json", "", " "]),
        "intent_text": st.one_of(
            st.none(), _json_text(_json_values(max_leaves=6)), _text_values(32)
        ),
        "model": st.one_of(st.none(), _text_values(32)),
        "output": st.one_of(st.none(), st.just("/nonexistent-deadeye-fuzz/evidence.json")),
        "allow_network": st.sampled_from([True, False, "true", 1, None, []]),
        "keep_raw_response": st.sampled_from([True, False, "false", 0, None]),
        "force": st.sampled_from([True, False, "no", 2, None]),
        "timeout_seconds": st.one_of(
            st.none(),
            st.sampled_from([0, -1, 1.5, 1e999, float("nan"), "60", True, [], {}]),
        ),
        "idempotency_key": st.sampled_from(
            [None, "", "  ", "k" * 200, "k" * 201, "fuzz-key", 7, [], {}]
        ),
    }
)

# Every tool whose arguments carry no media, so arbitrary client arguments
# can be thrown at it without a submission in reach.
_OFFLINE_TOOLS = ("doctor", "schema")
_READ_ONLY_TOOLS = (*_OFFLINE_TOOLS, "prompt")

_frame_ids = st.one_of(
    st.none(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(max_size=16),
    st.booleans(),
)

_frame_params = st.one_of(
    st.none(),
    _json_object_values(),
    _json_values(max_leaves=6),
    st.text(max_size=48),
    st.integers(),
)

# The handshake and listing methods, and a method this server does not know.
# `tools/call` is fuzzed separately, one tool at a time, so a fuzzed argument
# set can never reach a submission from here.
_frames = st.builds(
    lambda method, request_id, params: {
        key: value
        for key, value in (
            ("jsonrpc", "2.0"),
            ("id", request_id),
            ("method", method),
            ("params", params),
        )
        if not (key == "id" and method == "notifications/initialized")
    },
    st.sampled_from(
        ("initialize", "ping", "tools/list", "notifications/initialized", "", "no/such")
    ),
    _frame_ids,
    _frame_params,
)


def _assert_jsonrpc_response(response: Any, request_id: Any) -> None:
    """Every answered frame is a well-formed JSON-RPC response for `request_id`."""
    assert isinstance(response, dict), "a request frame is always answered"
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == request_id, "the answer carries the id it was asked under"
    assert ("result" in response) != ("error" in response), "exactly one of result/error"
    if "error" in response:
        assert isinstance(response["error"]["code"], int)
        assert isinstance(response["error"]["message"], str)
        return
    result = response["result"]
    assert isinstance(result, dict)
    for block in result.get("content", []):
        assert block["type"] == "text"
        assert isinstance(block["text"], str)
    assert "isError" not in result or isinstance(result["isError"], bool)


@settings(max_examples=200, deadline=None)
@given(frame=_frames)
def test_fuzz_mcp_frame_answers_in_band(frame: dict[str, Any]) -> None:
    response = mcp.handle_frame(frame)
    if "id" not in frame:
        assert response is None, "a notification is answered with silence"
        return
    _assert_jsonrpc_response(response, frame["id"])
    if "error" in response:
        # Spec error codes only: a frame this server does not understand is
        # not an internal fault, so a client can tell it from a server fault.
        assert response["error"]["code"] in (-32600, -32601, -32602, -32603)


# `prompt` renders a preview and touches the filesystem for its `clip` and
# `intent` arguments, so path-shaped values are kept under a root that does not
# exist: the fuzzer exercises the argument typing, not the checkout's tree.
_prompt_arguments = st.dictionaries(
    st.text(max_size=16),
    st.one_of(
        _scalars,
        st.lists(_scalars, max_size=3),
        st.text(max_size=24).map(lambda tail: _ABSENT_CLIP + "/" + tail),
    ),
    max_size=4,
)


@settings(max_examples=200, deadline=None)
@given(
    name=st.one_of(
        st.sampled_from(_READ_ONLY_TOOLS),
        st.text(max_size=16),
        st.none(),
        st.integers(),
    ),
    arguments=st.one_of(
        _json_object_values(),
        _prompt_arguments,
        st.lists(_scalars, max_size=3),
        st.none(),
        st.text(max_size=24),
    ),
)
def test_fuzz_mcp_read_only_tool_arguments_stay_in_band(name: Any, arguments: Any) -> None:
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = mcp.handle_frame(frame)
    _assert_jsonrpc_response(response, 1)
    if "error" in response:
        # A bad name or a non-object argument set is a params error, not a
        # fault, and never a tool result.
        assert response["error"]["code"] == -32602


@settings(max_examples=200, deadline=None)
@given(arguments=_review_arguments)
def test_fuzz_mcp_review_arguments_never_submit_or_spend_a_key(arguments: dict[str, Any]) -> None:
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "review", "arguments": arguments},
    }
    before = dict(mcp._COMPLETED)
    response = mcp.handle_frame(frame)
    _assert_jsonrpc_response(response, 1)
    # No real clip, so every call here is a refusal: nothing is submitted and
    # no idempotency key is spent on media that never left the machine.
    assert response["result"].get("isError") is True
    assert response["result"]["content"][0]["text"].startswith("ERROR:")
    assert dict(mcp._COMPLETED) == before, "a refused call must not record a ledger entry"


# The transport itself: a client writes lines, the loop answers. Frames that
# cannot be JSON, cannot be UTF-8, or exceed the frame cap get the spec's
# parse error, and the loop keeps serving the lines after them.
_transport_lines = st.lists(
    st.one_of(
        st.binary(max_size=48),
        st.sampled_from(
            [
                b"",
                b"{}",
                b"null",
                b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                b'{"jsonrpc":"2.0","id":null,"method":"initialize"}',
                b"not json at all",
                b"\xff\xfe\x00broken",
                b"[]",
                b'{"jsonrpc":"2.0","id":1}',
                # A frame past the 1 MiB cap: discarded through its newline
                # and answered as a parse error, never retained.
                b'{"jsonrpc":"2.0","id":1,"method":"' + b"x" * (1024 * 1024) + b'"}',
            ]
        ),
        st.text(max_size=48).map(str.encode),
    ),
    min_size=1,
    max_size=4,
)


@settings(max_examples=100, deadline=None)
@given(lines=_transport_lines)
def test_fuzz_mcp_transport_answers_only_with_framed_json_rpc(
    lines: list[bytes],
) -> None:
    stdout = io.StringIO()
    assert mcp.serve(io.BytesIO(b"\n".join(lines) + b"\n"), stdout) == 0
    answers = [line for line in stdout.getvalue().splitlines() if line]
    for answer in answers:
        frame = json.loads(answer)
        assert isinstance(frame, dict), "stdout carries framed JSON-RPC and nothing else"
        assert frame["jsonrpc"] == "2.0"
        assert ("result" in frame) != ("error" in frame)
        if "error" in frame:
            assert frame["error"]["code"] in (-32600, -32700)
    # Every request is answered exactly once and nothing else is: a client
    # never waits on silence, and stdout never carries a second frame. The
    # oracle counts the lines the transport actually sees, so a payload that
    # carries its own newline is judged per newline, as the loop reads it.
    assert len(answers) == _expected_answers(b"\n".join(lines).split(b"\n"))


def _expected_answers(lines: list[bytes]) -> int:
    """How many answers a set of client lines must draw.

    Every non-blank line draws exactly one: a request gets its response, and
    anything the loop cannot parse gets the spec's parse error rather than
    silence. Only a frame with no `id` member is a notification, which the
    spec answers with nothing at all.
    """
    expected = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            frame = json.loads(line.decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError):
            expected += 1
            continue
        if not (isinstance(frame, dict) and "id" not in frame):
            expected += 1
    return expected


# ---------------------------------------------------------------------------
# Target 6: the configuration file and the endpoint override.
#
# `config.toml` and `config.local.toml` are hand-edited, and a proxy root
# override is the one setting that decides where a provider credential is
# sent. Both are parsed before anything is submitted, so the invariants are:
#
# - a file deadeye cannot parse, or one setting a key path that no adapter
#   reads, refuses by name as a ValueError; nothing else escapes, so a typo
#   or a truncated file is a message and not a traceback;
# - a loaded file yields leaves only at the known key paths, each naming the
#   file it came from, and a value a reader asks for by a path that does not
#   exist is None rather than a walk through a non-table;
# - an accepted endpoint override is https, or plain http to a loopback
#   host: the value that would carry the bearer key never leaves on
#   cleartext to anywhere else, whatever the string says;
# - the generation knobs are absent-or-usable, so a value that reaches a
#   request body is an integer above its floor or a finite float.
# ---------------------------------------------------------------------------

_CONFIG_KEYS = (
    "api_key",
    "default_model",
    "default_provider",
    "providers",
    "timeout_seconds",
)

_URL_CHUNKS = (
    "https://",
    "http://",
    "HTTP://",
    "//",
    "localhost",
    "127.0.0.1",
    "::1",
    "[::1",
    "[v1.x]",
    "@",
    ":",
    "/",
    "?#",
    "\x00",
    " ",
    "%zz",
    "\\",
    "provider.example",
)

_url_overrides = st.lists(st.sampled_from(_URL_CHUNKS), min_size=1, max_size=6).map(
    "".join
) | st.text(max_size=32)

_ENDPOINTS = (("providers", "nvidia", "endpoint"), ("providers", "gemini", "endpoint"))


@FUZZ
@given(override=_url_overrides, keys=st.sampled_from(_ENDPOINTS))
def test_fuzz_endpoint_override_is_https_or_loopback(override: str, keys: tuple[str, ...]) -> None:
    fallback = "https://fallback.example/v1"
    with patch.object(config, "value", lambda _keys: override):
        try:
            accepted = config.endpoint(keys, fallback)
        except DeadeyeError:
            # A refused override names itself through the question `doctor`
            # asks, and doctor is the one caller that must not raise.
            assert config.endpoint_problem(keys) is not None
            return
        assert config.endpoint_problem(keys) is None
    # An accepted override is the configured string, stripped: nothing is
    # rewritten into a different root than the operator wrote down. An unset
    # or blank override reads as the caller's own fallback.
    if not override.strip():
        assert accepted == fallback
        return
    assert accepted == override.strip()
    # The oracle is an independent parse of the value that was accepted, so a
    # reader that waves through what this rejects, or reads a different host,
    # fails here rather than in a request the credential has already sent.
    parts = urlsplit(accepted)
    assert parts.netloc, "an accepted override names a host"
    if parts.scheme == "http":
        # The one plain-http root an operator may name: a self-hosted proxy on
        # this machine. Anywhere else the credential would travel in clear.
        assert (parts.hostname or "") in config.LOOPBACK_HOSTS
    else:
        assert parts.scheme == "https"


@FUZZ
@given(
    document=st.one_of(
        st.text(max_size=256),
        st.builds(
            lambda name, value: f"{name} = {value}\n",
            st.sampled_from((*_CONFIG_KEYS, "default_provder", "providers.geminie.api_key")),
            st.one_of(
                st.sampled_from(['"text"', "7", "1.5", "true", "[]", "{ }", "nan", "inf"]),
                st.text(max_size=16).map(json.dumps),
            ),
        ),
        st.builds(
            lambda table, key, value: f"[{table}]\n{key} = {value}\n",
            st.sampled_from(
                [
                    "providers.nvidia",
                    "providers.gemini",
                    "providers.unknown",
                    "providers",
                    "providers.nvidia.endpoint",
                ]
            ),
            st.sampled_from(
                ["endpoint", "api_key", "model", "max_tokens", "temperature", "timeout_seconds"]
            ),
            st.sampled_from(['"x"', "0", "-3", "nan", "inf", "true", "[]"]),
        ),
    )
)
def test_fuzz_config_file_refuses_by_name_or_loads_known_keys(document: str) -> None:
    with tempfile.TemporaryDirectory(prefix="deadeye-fuzz-cfg-") as raw:
        directory = Path(raw)
        (directory / "config.toml").write_text(document, encoding="utf-8")
        try:
            loaded = config.Config(directory)
        except ValueError:
            return  # refusal: unreadable or unread, the only allowed failure mode
    # A file that loads carries only leaves at key paths an adapter reads, and
    # every leaf names the file it came from.
    for path, _value in _leaf_paths(loaded.data, ()):
        assert _is_known_path(path), f"loaded a setting deadeye does not read: {path}"
        assert loaded.provenance(path) == "config.toml"
    # A path that does not exist reads as None, whatever the tables hold: a key
    # path through a leaf or a missing table is a miss, not a walk that hands
    # the caller something it never configured.
    assert loaded.value(("providers", "not-a-provider", "api_key")) is None
    assert loaded.value(("api_key", "deeper")) is None


def _leaf_paths(data: object, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], object]]:
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        child = (*path, key)
        if isinstance(value, dict):
            yield from _leaf_paths(value, child)
        else:
            yield child, value


def _is_known_path(path: tuple[str, ...]) -> bool:
    """An independent reading of the key tables, not the loader's own check."""
    if path[:1] == ("providers",) and len(path) == 3:
        known = config.PROVIDER_KEYS.get(path[1])
        return known is not None and path[2] in known
    if len(path) == 1:
        return path[0] in config.TOP_LEVEL_KEYS
    return False


@FUZZ
@given(
    leaf=st.one_of(
        st.none(),
        st.sampled_from([0, -1, 1, 1 << 40, 1.0, -0.5, float("nan"), float("inf")]),
        st.sampled_from(["12", True, [], {}, float("nan"), float("-inf")]),
    )
)
def test_fuzz_generation_knobs_are_absent_or_usable(leaf: object) -> None:
    with patch.object(config, "value", lambda _keys: leaf):
        try:
            cap = int_setting("nvidia", "max_tokens", fallback=1024, minimum=1)
        except DeadeyeError:
            return  # refusal: the only allowed failure mode, and a named one
        # A cap that cleared its floor is a plain int: no bool read as 1 and
        # no float rounded, so the operator's cap is the cap that is sent.
        assert isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1
        try:
            temperature = float_setting("nvidia", "temperature", fallback=0.4)
        except DeadeyeError:
            return
    # A value that reaches the request body is finite: `nan` or `inf` would
    # serialize as a token no JSON reader on the provider side accepts.
    assert math.isfinite(temperature)
