"""The stable result shape every deadeye review returns.

This is the vision side of the one result family the hordeforge review tools
share with the audio-review pipeline: `summary`, `strengths`, `issues`,
`recommended_changes`, `rubric_scores`, `confidence`, `limitations`. A caller
that handles both review kinds reads one shape and does not branch on whether
a critique was of a sound or a mesh.

Video issues may name a moment two ways: `at_seconds` (seconds from clip
start, the convention the audio side uses) and/or `at_frame` (the sampled
frame index). A caller reads whichever is present; both are validated, and a
review that drops frames to fit a provider budget records the sampling
separately (see `sampling.py`) so a frame index is never mistaken for a
wall-clock time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import DeadeyeError

RUBRIC_VERSION = "1"
PROMPT_VERSION = "1"

RESULT_KEYS = (
    "summary",
    "strengths",
    "issues",
    "recommended_changes",
    "rubric_scores",
    "confidence",
    "limitations",
)

ADVISORY_NOTE = (
    "Advisory only: a model critique is evidence about the submitted media "
    "under the recorded intent. It cannot mark an asset accepted; human "
    "sign-off in the real context decides that."
)


@dataclass(frozen=True)
class RubricDimension:
    """One property every video review scores, and what a low score means."""

    key: str
    question: str


# The dimensions mirror the asset-pipeline PRD's desired qualities and avoid
# list: proportions, silhouette, material read, timing, and the motion failures
# a single still cannot show (clipping, popping, z-fighting, wrong scale,
# jitter).
BASE_RUBRIC: tuple[RubricDimension, ...] = (
    RubricDimension("semantic_fit", "does it fit the stated purpose"),
    RubricDimension("proportions", "are the proportions right for the stated subject"),
    RubricDimension("silhouette_read", "does the silhouette read correctly at a glance"),
    RubricDimension("material_read", "does the surface read as the material it claims"),
    RubricDimension("motion_plausibility", "is the motion plausible for the stated subject"),
    RubricDimension("timing", "is the timing deliberate and readable"),
    RubricDimension("clipping_risk", "does anything clip, intersect, or pass through"),
    RubricDimension("popping_risk", "does anything pop, snap, or teleport"),
    RubricDimension("scale_risk", "does anything read at the wrong scale"),
    RubricDimension("z_fighting_risk", "is there z-fighting or shimmer at surfaces"),
    RubricDimension("jitter_risk", "is there jitter, stutter, or camera noise"),
    RubricDimension("lighting_read", "does lighting help or fight the read"),
)


def parse_model_json(raw_text: str) -> dict[str, Any]:
    """Extract the JSON object from a model response, refusing anything else."""
    text = raw_text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeadeyeError(
            f"model returned invalid structure (not JSON): {exc}; rerun with "
            "--keep-raw-response to preserve a redacted copy for debugging"
        ) from exc
    if not isinstance(parsed, dict):
        raise DeadeyeError(
            "model returned invalid structure (a JSON "
            f"{type(parsed).__name__}, not an object); rerun with "
            "--keep-raw-response to preserve a redacted copy for debugging"
        )
    return parsed


def validate_result(
    data: dict[str, Any],
    dimensions: tuple[RubricDimension, ...] = BASE_RUBRIC,
    origin: str = "model response",
) -> dict[str, Any]:
    """Normalize a model answer into the pipeline-owned result shape.

    Every deviation is a hard failure naming what was wrong: a silently
    coerced field would put words into the reviewer's mouth. Scores are
    validated as diagnostics in 0-5 or an explicit null; a null should be
    explained under `limitations` by convention, but the shape alone does not
    enforce that.
    """
    problems: list[str] = []
    missing = [key for key in RESULT_KEYS if key not in data]
    if missing:
        problems.append(f"missing key(s): {', '.join(missing)}")
    extra = sorted(set(data) - set(RESULT_KEYS))
    if extra:
        problems.append(f"unexpected key(s): {', '.join(extra)}")
    if problems:
        raise DeadeyeError(f"{origin} returned an invalid structure: {'; '.join(problems)}")

    def strings(key: str) -> list[str]:
        value = data[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            problems.append(f"{key} must be an array of strings")
            return []
        return [item for item in value if item.strip()]

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be a non-empty string")

    issues: list[dict[str, Any]] = []
    raw_issues = data["issues"]
    if not isinstance(raw_issues, list):
        problems.append("issues must be an array")
    else:
        for index, entry in enumerate(raw_issues):
            if not isinstance(entry, dict) or "description" not in entry:
                problems.append(f"issue #{index + 1} must be an object with 'description'")
                continue
            unexpected = sorted(set(entry) - {"description", "at_seconds", "at_frame"})
            if unexpected:
                problems.append(
                    f"issue #{index + 1} has unexpected key(s): {', '.join(unexpected)}"
                )
                continue
            description = entry["description"]
            if not isinstance(description, str) or not description.strip():
                problems.append(f"issue #{index + 1} needs a non-empty description")
                continue
            issue: dict[str, Any] = {"description": description.strip()}
            seconds = entry.get("at_seconds")
            if seconds is not None:
                valid_seconds = (
                    isinstance(seconds, list)
                    and len(seconds) == 2
                    and all(isinstance(bound, (int, float)) for bound in seconds)
                    and seconds[0] <= seconds[1]
                )
                if not valid_seconds:
                    problems.append(
                        f"issue #{index + 1} at_seconds must be [start, end] numbers "
                        "with start <= end"
                    )
                    continue
                issue["at_seconds"] = [float(seconds[0]), float(seconds[1])]
            frame = entry.get("at_frame")
            if frame is not None:
                valid_frame = (
                    isinstance(frame, list)
                    and len(frame) == 2
                    and all(isinstance(bound, (int, float)) and bound >= 0 for bound in frame)
                    and frame[0] <= frame[1]
                )
                if not valid_frame:
                    problems.append(
                        f"issue #{index + 1} at_frame must be [start, end] non-negative "
                        "numbers with start <= end"
                    )
                    continue
                issue["at_frame"] = [float(frame[0]), float(frame[1])]
            if "at_seconds" not in issue and "at_frame" not in issue:
                # Not a hard failure: the model may be describing a quality of
                # the whole clip rather than one moment. The convention is to
                # place what you can, but the shape stays permissive.
                pass
            issues.append(issue)

    known = {item.key for item in dimensions}
    scores: dict[str, float | None] = {}
    raw_scores = data["rubric_scores"]
    if not isinstance(raw_scores, dict):
        problems.append("rubric_scores must be an object keyed by rubric dimension")
    else:
        for key, value in raw_scores.items():
            if key not in known:
                problems.append(
                    f"rubric_scores names unknown dimension {key!r}; expected: "
                    + ", ".join(sorted(known))
                )
                continue
            if value is None:
                scores[key] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"rubric_scores[{key!r}] must be a number or null")
            elif not 0 <= value <= 5:
                problems.append(f"rubric_scores[{key!r}] must be within 0-5")
            else:
                scores[key] = float(value)

    confidence = data["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        problems.append("confidence must be a number between 0 and 1")

    if problems:
        raise DeadeyeError(
            f"{origin} returned an invalid structure (schema mismatch): " + "; ".join(problems)
        )
    return {
        "summary": summary.strip(),
        "strengths": strings("strengths"),
        "issues": issues,
        "recommended_changes": strings("recommended_changes"),
        "rubric_scores": scores,
        "confidence": round(float(confidence), 4),
        "limitations": strings("limitations"),
    }
