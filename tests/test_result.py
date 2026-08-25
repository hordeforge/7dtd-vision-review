"""Result-schema validation: every deviation is a hard failure."""

from __future__ import annotations

import pytest

from deadeye.errors import DeadeyeError
from deadeye.result import RESULT_KEYS, parse_model_json, validate_result

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


def test_model_json_is_extracted_from_fences_and_refuses_non_json() -> None:
    assert parse_model_json(f"```json\n{__import__('json').dumps(VALID)}\n```") == VALID
    with pytest.raises(DeadeyeError, match="not JSON"):
        parse_model_json("I think it looks fine.")
    with pytest.raises(DeadeyeError, match="not an object"):
        parse_model_json("[1, 2, 3]")


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
