"""Property-based fuzz targets for the two untrusted-input parsers.

Every provider response (raw model text) and every intent document is data
from outside this process: a third party's answer, or an authored file that
may be malformed by accident or on purpose. These harnesses fuzz the parsers
that consume those inputs with Hypothesis (structure-aware strategies, not
blind byte mutation) and pin the invariants the pipeline depends on:

- only DeadeyeError may refuse input; any other exception escapes and fails
  the run, so a crash on malformed data is visible instead of silent;
- anything that IS accepted must satisfy the pipeline-owned shape exactly,
  including across a re-validation round trip;
- `redact` must drop every credential-bearing key from any JSON-shaped
  value, however deeply it is buried: the load-bearing credentials backstop.

Run with the rest of the suite (`make test`). A failure prints the
falsifying example: pin it as a regression test next to the parser's unit
tests before changing anything. On a bare host without the dev group
(no uv), this module skips itself rather than aborting collection.
"""

from __future__ import annotations

import json
import math

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
from deadeye.intent import SENSITIVE_KEY_PARTS, parse_intent, parse_intent_text, redact
from deadeye.result import BASE_RUBRIC, RESULT_KEYS, parse_model_json, validate_result

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


def _assert_pipeline_shape(result: dict) -> None:
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
    lowered = key.lower()
    return lowered == "key" or any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _walk_keys(value: object):
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


@FUZZ
@given(_json_values(max_leaves=14))
def test_fuzz_redact_drops_every_sensitive_key(value: object) -> None:
    cleaned = redact(value)
    for key in _walk_keys(cleaned):
        assert isinstance(key, str), "redact drops non-string keys"
        assert not _looks_sensitive(key), f"credential-bearing key {key!r} survived redact"
    # Redaction is idempotent: nothing new to remove on a second pass.
    assert redact(cleaned) == cleaned


@FUZZ
@given(st.one_of(_intent_docs, st.text(max_size=128)))
def test_fuzz_intent_parse_never_crashes_and_round_trips(document: object) -> None:
    try:
        text = json.dumps(document) if not isinstance(document, str) else document
        intent = parse_intent_text(text)[0]
    except DeadeyeError:
        return  # refusal: the only allowed failure mode
    assert isinstance(intent.purpose, str) and intent.purpose.strip()
    assert intent.camera_path == "" or isinstance(intent.camera_path, str)
    for field in (intent.avoid, intent.questions):
        assert all(isinstance(item, str) and item.strip() for item in field)
    for reference in intent.references:
        assert reference.path != ""
        assert isinstance(reference.purpose, str) and reference.purpose.strip()
    # Round trip across the persistence boundary: the shape written by
    # as_dict must read back identical.
    assert parse_intent(intent.as_dict(), "round-trip").as_dict() == intent.as_dict()
