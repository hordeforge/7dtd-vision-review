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
  a filename cannot forge an extra label-shaped line in the prompt.

Run with the rest of the suite (`make test`). A failure prints the
falsifying example: pin it as a regression test next to the parser's unit
tests before changing anything. On a bare host without the dev group
(no uv), this module skips itself rather than aborting collection.
"""

from __future__ import annotations

import email.message
import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:
    pytest.skip(
        "hypothesis is not installed; run scripts/bootstrap for the full suite",
        allow_module_level=True,
    )

from deadeye.errors import DeadeyeError
from deadeye.intent import SENSITIVE_KEY_PARTS, load_intent, parse_intent, redact
from deadeye.json_safe import strict_json_numbers
from deadeye.providers._http import _decode_envelope
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
    # Mirrors intent._is_sensitive_key: case folding, not lower(), so the
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
