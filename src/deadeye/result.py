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
import math
import re
from dataclasses import dataclass
from typing import Any

from .errors import DeadeyeError

RUBRIC_VERSION = "1"
# 2: the author statement became a fenced, data-only block (prompt.py).
PROMPT_VERSION = "2"

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


def _moment(value: Any, key: str, *, non_negative: bool) -> list[float] | None:
    """Normalize an issue moment: `[start, end]` or a single value -> `[n, n]`.

    Models point at a moment with either shape; a single frame index or
    second is the natural way to name one frame, and refusing it would put a
    hard failure on a legitimate answer. Returns None when the value is
    present but neither shape is valid. Non-finite floats (`NaN`, the
    infinities) are refused: they would survive into evidence JSON that no
    strict JSON reader can parse.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or (non_negative and value < 0):
            return None
        return [float(value), float(value)]
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(bound, (int, float)) and not isinstance(bound, bool) and math.isfinite(bound)
            for bound in value
        )
        and (not non_negative or value[0] >= 0)
        and value[0] <= value[1]
    ):
        return [float(value[0]), float(value[1])]
    return None


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
    except RecursionError as exc:
        # A response nested beyond the interpreter limit is a malformed
        # answer, not a bug here: refuse it like any other bad structure.
        raise DeadeyeError(
            "model returned invalid structure (nested too deeply); rerun with "
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
    if not isinstance(data, dict):
        # A sequence holding exactly the result key names would slip past the
        # key-set checks below and die on subscripting; refuse it here.
        raise DeadeyeError(f"{origin} returned an invalid structure: not a JSON object")
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
            # The live NVIDIA model names a moment with the singular aliases
            # `frame` / `seconds` as often as the canonical `at_frame` /
            # `at_seconds`; normalize them before the shape check so a real
            # verdict is not thrown away for a naming variant.
            if "frame" in entry:
                entry.setdefault("at_frame", entry.pop("frame"))
            if "seconds" in entry:
                entry.setdefault("at_seconds", entry.pop("seconds"))
            # Start/end pairs: {"start_frame": 9, "end_frame": 11} is the
            # same moment as {"at_frame": [9, 11]}.
            start, end = entry.pop("start_frame", None), entry.pop("end_frame", None)
            if "at_frame" not in entry and start is not None and end is not None:
                entry["at_frame"] = [start, end]
            start, end = entry.pop("start_seconds", None), entry.pop("end_seconds", None)
            if "at_seconds" not in entry and start is not None and end is not None:
                entry["at_seconds"] = [start, end]
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
            seconds = _moment(entry.get("at_seconds"), "at_seconds", non_negative=False)
            if "at_seconds" in entry and entry["at_seconds"] is not None and seconds is None:
                problems.append(
                    f"issue #{index + 1} at_seconds must be [start, end] numbers "
                    "with start <= end, or a single second"
                )
                continue
            if seconds is not None:
                issue["at_seconds"] = seconds
            frame = _moment(entry.get("at_frame"), "at_frame", non_negative=True)
            if "at_frame" in entry and entry["at_frame"] is not None and frame is None:
                problems.append(
                    f"issue #{index + 1} at_frame must be [start, end] non-negative "
                    "numbers with start <= end, or a single frame index"
                )
                continue
            if frame is not None:
                issue["at_frame"] = frame
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
