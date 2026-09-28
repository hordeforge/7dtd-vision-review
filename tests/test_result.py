"""Result-schema validation: every deviation is a hard failure."""

from __future__ import annotations

import pytest

from deadeye.errors import DeadeyeError
from deadeye.result import (
    MAX_LIST_ITEMS,
    MAX_TEXT_CHARS,
    RESULT_KEYS,
    parse_model_json,
    validate_result,
)

VALID = {
    "summary": "reads well in motion",
    "strengths": ["silhouette holds through the turn"],
    "issues": [
        {"description": "clips at the shoulder", "at_seconds": [2.0, 3.0], "at_frame": [8, 12]}
    ],
    "recommended_changes": ["taper the shoulder seam"],
    "rubric_scores": {"semantic_fit": 4, "motion_plausibility": 2.5},
    "confidence": 0.8,
    "limitations": ["lighting without the engine"],
}


def test_a_valid_result_normalizes() -> None:
    result = validate_result(VALID)
    assert result["summary"] == VALID["summary"]
    assert result["issues"][0]["at_seconds"] == [2.0, 3.0]
    assert result["issues"][0]["at_frame"] == [8.0, 12.0]
    assert result["confidence"] == 0.8


def test_missing_keys_are_refused() -> None:
    for key in RESULT_KEYS:
        data = dict(VALID)
        del data[key]
        with pytest.raises(DeadeyeError, match="missing key"):
            validate_result(data)


def test_extra_keys_are_refused() -> None:
    with pytest.raises(DeadeyeError, match="unexpected key"):
        validate_result({**VALID, "verdict": "pass"})


def test_summary_must_be_non_empty() -> None:
    with pytest.raises(DeadeyeError, match="summary must be a non-empty string"):
        validate_result({**VALID, "summary": "  "})


@pytest.mark.parametrize("issues", [{"description": "clips"}, "clips", 7, None], ids=str)
def test_issues_must_be_an_array_of_issue_objects(issues: object) -> None:
    """The issue list is the only place a model names a defect, so a payload
    that is not a list of objects has no reading: it is refused, never coerced
    into an empty list that would report a clean clip."""
    with pytest.raises(DeadeyeError, match="issues must be an array"):
        validate_result({**VALID, "issues": issues})


@pytest.mark.parametrize(
    "entry", ["clips at the shoulder", 7, None, {"at_seconds": [1.0]}], ids=str
)
def test_an_issue_without_a_description_is_refused(entry: object) -> None:
    """A bare string, a number, or a moment with no description is a shape the
    validator does not repair: the entry is dropped and the whole answer fails,
    because a verdict missing the defect text is worse than no verdict."""
    with pytest.raises(DeadeyeError, match=r"issue #1 must be an object with 'description'"):
        validate_result({**VALID, "issues": [entry]})


@pytest.mark.parametrize("description", ["", "   ", 7, None], ids=str)
def test_an_issue_description_must_be_a_non_empty_string(description: object) -> None:
    with pytest.raises(DeadeyeError, match="issue #1 needs a non-empty description"):
        validate_result({**VALID, "issues": [{"description": description}]})


@pytest.mark.parametrize("rubric_scores", ["high", [4, 5], None], ids=str)
def test_rubric_scores_must_be_an_object(rubric_scores: object) -> None:
    """A rubric that is not an object cannot be read dimension by dimension,
    and defaulting it to empty would report every dimension unmeasured on an
    answer that did carry scores."""
    with pytest.raises(DeadeyeError, match="rubric_scores must be an object"):
        validate_result({**VALID, "rubric_scores": rubric_scores})


@pytest.mark.parametrize("key", ["strengths", "recommended_changes", "limitations"], ids=str)
def test_string_list_fields_are_refused_not_silently_emptied(key: str) -> None:
    with pytest.raises(DeadeyeError, match=f"{key} must be an array of strings"):
        validate_result({**VALID, key: ["fine", 7]})


def test_issue_moments_are_validated() -> None:
    with pytest.raises(DeadeyeError, match="at_seconds must be"):
        validate_result({**VALID, "issues": [{"description": "x", "at_seconds": [3.0, 2.0]}]})
    with pytest.raises(DeadeyeError, match="at_frame must be"):
        validate_result({**VALID, "issues": [{"description": "x", "at_frame": [-1, 2]}]})
    with pytest.raises(DeadeyeError, match="unexpected key"):
        validate_result({**VALID, "issues": [{"description": "x", "at_segment": [0, 1]}]})
    # A whole-clip issue without a moment is allowed.
    result = validate_result({**VALID, "issues": [{"description": "reads small"}]})
    assert result["issues"][0] == {"description": "reads small"}


def test_scores_are_diagnostic_0_5_or_null() -> None:
    result = validate_result({**VALID, "rubric_scores": {"semantic_fit": None, "clipping_risk": 0}})
    assert result["rubric_scores"] == {"semantic_fit": None, "clipping_risk": 0.0}
    with pytest.raises(DeadeyeError, match="unknown dimension"):
        validate_result({**VALID, "rubric_scores": {"taste": 3}})
    with pytest.raises(DeadeyeError, match="within 0-5"):
        validate_result({**VALID, "rubric_scores": {"semantic_fit": 6}})
    with pytest.raises(DeadeyeError, match="number or null"):
        validate_result({**VALID, "rubric_scores": {"semantic_fit": True}})


def test_confidence_must_be_between_0_and_1() -> None:
    with pytest.raises(DeadeyeError, match="confidence must be"):
        validate_result({**VALID, "confidence": 1.5})


def test_a_verdict_holding_more_than_its_share_is_refused() -> None:
    """A model that filled the box instead of answering is refused, not trimmed.

    The transport bounds the whole response and the generation cap bounds the
    tokens, but nothing bounded what one verdict was: a multi-megabyte summary
    or thousands of issues passed every type check and reached the evidence
    file, stdout, and the MCP ledger, where a person reads it."""
    assert validate_result(VALID)["summary"] == "reads well in motion"

    with pytest.raises(DeadeyeError, match="summary must be at most"):
        validate_result({**VALID, "summary": "x" * (MAX_TEXT_CHARS + 1)})

    with pytest.raises(DeadeyeError, match="issue #1 description must be at most"):
        validate_result({**VALID, "issues": [{"description": "x" * (MAX_TEXT_CHARS + 1)}]})

    with pytest.raises(DeadeyeError, match="strengths must hold at most"):
        validate_result({**VALID, "strengths": ["holds"] * (MAX_LIST_ITEMS + 1)})

    with pytest.raises(DeadeyeError, match="issues must hold at most"):
        validate_result({**VALID, "issues": [{"description": "clips"}] * (MAX_LIST_ITEMS + 1)})

    with pytest.raises(DeadeyeError, match="limitations entries longer than"):
        validate_result({**VALID, "limitations": ["x" * (MAX_TEXT_CHARS + 1)]})


def test_a_verdict_at_the_cap_still_normalizes() -> None:
    """The caps refuse; they never cut. A value exactly at the limit is a real
    answer and must survive unchanged, so the boundary is inclusive."""
    summary = "x" * MAX_TEXT_CHARS
    assert validate_result({**VALID, "summary": summary})["summary"] == summary
    entry = "holds"
    at_cap = {**VALID, "strengths": [entry] * MAX_LIST_ITEMS}
    assert len(validate_result(at_cap)["strengths"]) == MAX_LIST_ITEMS


def test_model_json_is_extracted_from_fences_and_refuses_non_json() -> None:
    assert parse_model_json(f"```json\n{__import__('json').dumps(VALID)}\n```") == VALID
    with pytest.raises(DeadeyeError, match="not JSON"):
        parse_model_json("I think it looks fine.")
    with pytest.raises(DeadeyeError, match="not an object"):
        parse_model_json("[1, 2, 3]")


def test_deeply_nested_model_output_is_refused_not_crashed() -> None:
    # Nesting beyond the interpreter limit is a malformed answer, not a bug
    # in the parser: it must be refused like any other bad structure.
    with pytest.raises(DeadeyeError):
        parse_model_json("[" * 20000 + "]" * 20000)


def test_a_non_object_is_refused_even_when_it_names_the_keys() -> None:
    # A sequence holding exactly the seven key names passes the key-set
    # checks but cannot be subscripted; it must be refused up front.
    with pytest.raises(DeadeyeError, match="not a JSON object"):
        validate_result(list(RESULT_KEYS))


def test_a_single_frame_index_or_second_normalizes_to_a_pair() -> None:
    result = validate_result({**VALID, "issues": [{"description": "pops at 10", "at_frame": 10}]})
    assert result["issues"][0]["at_frame"] == [10.0, 10.0]
    result = validate_result(
        {**VALID, "issues": [{"description": "starts at 2s", "at_seconds": 2}]}
    )
    assert result["issues"][0]["at_seconds"] == [2.0, 2.0]
    # A negative single frame index is still refused.
    with pytest.raises(DeadeyeError, match="at_frame must be"):
        validate_result({**VALID, "issues": [{"description": "x", "at_frame": -1}]})


def test_an_explicit_null_moment_is_allowed_like_an_absent_one() -> None:
    data = {
        **VALID,
        "issues": [{"description": "whole-clip read", "at_frame": None, "at_seconds": None}],
    }
    result = validate_result(data)
    assert result["issues"][0] == {"description": "whole-clip read"}


def test_non_finite_moments_are_refused() -> None:
    # json.loads accepts NaN/Infinity literals; they would survive into
    # evidence JSON that no strict reader can parse.
    for probe in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(DeadeyeError, match="at_seconds must be"):
            validate_result({**VALID, "issues": [{"description": "x", "at_seconds": probe}]})
        with pytest.raises(DeadeyeError, match="at_frame must be"):
            validate_result({**VALID, "issues": [{"description": "x", "at_frame": probe}]})
    with pytest.raises(DeadeyeError, match="at_frame must be"):
        validate_result({**VALID, "issues": [{"description": "x", "at_frame": [0, float("inf")]}]})


def test_moments_too_large_for_a_double_are_refused_not_crashed() -> None:
    # json.loads hands a bare integer literal to Python as an int of any size,
    # and both math.isfinite() and float() raise OverflowError on one past
    # 2^1024. A model answering `at_frame` with 400 digits must land on the
    # refusal, not unwind the validator on a submission that has been billed.
    huge = 10**400
    for moment in (huge, [0, huge], [huge, huge]):
        with pytest.raises(DeadeyeError, match="at_frame must be"):
            validate_result({**VALID, "issues": [{"description": "x", "at_frame": moment}]})
        with pytest.raises(DeadeyeError, match="at_seconds must be"):
            validate_result({**VALID, "issues": [{"description": "x", "at_seconds": moment}]})


def test_a_moment_pair_is_ordered_on_the_values_as_written() -> None:
    # Past 2^53 two distinct integers are the same double, so ordering the
    # narrowed floats would wave through a reversed pair. The comparison is
    # on what the model wrote.
    with pytest.raises(DeadeyeError, match="at_frame must be"):
        validate_result(
            {
                **VALID,
                "issues": [{"description": "x", "at_frame": [2**53 + 1, 2**53]}],
            }
        )
    result = validate_result(
        {**VALID, "issues": [{"description": "x", "at_frame": [2**53, 2**53 + 1]}]}
    )
    assert result["issues"][0]["at_frame"] == [float(2**53), float(2**53)]


def test_the_singular_moment_aliases_normalize() -> None:
    result = validate_result({**VALID, "issues": [{"description": "pops", "frame": 9}]})
    assert result["issues"][0]["at_frame"] == [9.0, 9.0]
    result = validate_result({**VALID, "issues": [{"description": "starts", "seconds": 2.5}]})
    assert result["issues"][0]["at_seconds"] == [2.5, 2.5]
    # Canonical keys win over aliases when both are present.
    result = validate_result(
        {**VALID, "issues": [{"description": "x", "at_frame": [1, 2], "frame": 9}]}
    )
    assert result["issues"][0]["at_frame"] == [1.0, 2.0]


def test_start_end_pairs_normalize_to_moment_ranges() -> None:
    result = validate_result(
        {**VALID, "issues": [{"description": "warp", "start_frame": 9, "end_frame": 11}]}
    )
    assert result["issues"][0]["at_frame"] == [9.0, 11.0]
    result = validate_result(
        {**VALID, "issues": [{"description": "warp", "start_seconds": 1.0, "end_seconds": 3.5}]}
    )
    assert result["issues"][0]["at_seconds"] == [1.0, 3.5]


def test_normalizing_never_rewrites_the_model_payload() -> None:
    """The aliases and start/end pairs are normalized on a copy.

    `data` is the parsed model response, which the caller may still hold (to
    keep beside a preserved raw response, or to re-validate). Validating must
    not consume the names the model wrote, and re-validating the same dict
    must give the same answer.
    """
    payload = {
        **VALID,
        "issues": [{"description": "pops", "frame": 9, "start_seconds": 1.0, "end_seconds": 3.5}],
    }
    first = validate_result(payload)
    assert payload["issues"] == [
        {"description": "pops", "frame": 9, "start_seconds": 1.0, "end_seconds": 3.5}
    ]
    assert first["issues"][0] == {
        "description": "pops",
        "at_frame": [9.0, 9.0],
        "at_seconds": [1.0, 3.5],
    }
    assert validate_result(payload) == first


def test_a_lone_half_of_a_start_end_pair_is_refused_not_dropped() -> None:
    """A boundary with no other says nothing about a moment; discarding it
    would silently lose where the model pointed."""
    with pytest.raises(DeadeyeError, match="needs start_frame and end_frame together"):
        validate_result({**VALID, "issues": [{"description": "warp", "start_frame": 9}]})
    with pytest.raises(DeadeyeError, match="needs start_seconds and end_seconds together"):
        validate_result({**VALID, "issues": [{"description": "warp", "end_seconds": 3.5}]})


def test_a_canonical_moment_wins_over_a_full_start_end_pair() -> None:
    result = validate_result(
        {
            **VALID,
            "issues": [
                {
                    "description": "warp",
                    "at_frame": [1, 2],
                    "start_frame": 9,
                    "end_frame": 11,
                }
            ],
        }
    )
    assert result["issues"][0]["at_frame"] == [1.0, 2.0]
