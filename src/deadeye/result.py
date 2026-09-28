"""The stable result shape every deadeye review returns.

This is the vision side of the one result family the hordeforge review tools
share with the audio-review pipeline: `summary`, `strengths`, `issues`,
`recommended_changes`, `rubric_scores`, `confidence`, `limitations`. A caller
that handles both review kinds reads one shape and does not branch on whether
a critique was of a sound or a mesh.

Video issues may name a moment two ways: `at_seconds` (seconds from clip
start, the convention the audio side uses) and/or `at_frame` (the index of
the frame among those submitted, in attachment order, as
`prompt.FRAME_TIMING_NOTE` tells the model). A caller reads whichever is
present; both are validated. A review that drops frames to fit a provider
budget records the submitted frames' positions in the clip's own order
separately (`sampling.frame_indices`, see `sampling.py`), which is the map
from a submitted index back to a clip frame, so an `at_frame` is never
mistaken for a clip position or for a wall-clock time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import DeadeyeError
from .json_safe import finite_float

RUBRIC_VERSION = "1"

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

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
"""The fenced-JSON form a model wraps its verdict in, compiled once at import."""

# What one accepted verdict may hold. The provider response is already bounded
# at 8 MiB by the transport and at `max_output_tokens` by the generation cap,
# but nothing bounded what one verdict *is*: a 7 MiB summary, or fifty
# thousand issues, passed every check below and then reached the evidence file,
# stdout, and the MCP ledger, where a human reads it. These are refusal
# thresholds, not trimming thresholds, for the reason the rest of this module
# refuses rather than coerces: a cut list is a list the model never said. They
# sit far above a real review (a 12-dimension rubric with a paragraph per
# score is a few thousand characters) and far below the transport bound, so
# what they catch is a model that stopped answering the question and started
# filling the box.
MAX_TEXT_CHARS = 20_000
"""Longest single free-text value in a verdict, in characters."""

MAX_LIST_ITEMS = 200
"""Longest array in a verdict, in entries (issues, strengths, changes, limits)."""


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


def _moment(value: Any, *, non_negative: bool) -> list[float] | None:
    """Normalize an issue moment: `[start, end]` or a single value -> `[n, n]`.

    Models point at a moment with either shape; a single frame index or
    second is the natural way to name one frame, and refusing it would put a
    hard failure on a legitimate answer. Returns None when the value is
    present but neither shape is valid. Non-finite floats (`NaN`, the
    infinities) and integers too large for a double are refused: the first
    would survive into evidence JSON that no strict JSON reader can parse,
    and the second cannot be narrowed at all.
    """
    single = finite_float(value)
    if single is not None:
        if non_negative and single < 0:
            return None
        return [single, single]
    if not isinstance(value, list) or len(value) != 2:
        return None
    start = finite_float(value[0])
    end = finite_float(value[1])
    if start is None or end is None:
        return None
    # Ordered on the values as written, not on the doubles they narrow to: two
    # distinct frame indices past 2^53 are the same float, and a reversed pair
    # has to be refused on the integers the model actually wrote.
    if (non_negative and value[0] < 0) or value[0] > value[1]:
        return None
    return [start, end]


def parse_model_json(raw_text: str) -> dict[str, Any]:
    """Extract the JSON object from a model response, refusing anything else."""
    text = raw_text.strip()
    fenced = _FENCED_JSON_RE.search(text)
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


def validate_result(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a model answer into the pipeline-owned result shape.

    Every deviation is a hard failure naming what was wrong: a silently
    coerced field would put words into the reviewer's mouth. Scores are
    validated as diagnostics in 0-5 or an explicit null; a null should be
    explained under `limitations` by convention, but the shape alone does not
    enforce that.

    `data` is read, never rewritten: the alias and start/end normalizations
    below run on a copy of each issue, so the model payload the caller still
    holds is the model payload, and validating the same answer twice cannot
    normalize it twice.
    """
    origin = "model response"
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
        if len(value) > MAX_LIST_ITEMS:
            problems.append(f"{key} must hold at most {MAX_LIST_ITEMS} entries")
            return []
        oversized = [index for index, item in enumerate(value, 1) if len(item) > MAX_TEXT_CHARS]
        if oversized:
            problems.append(
                f"{key} entries longer than {MAX_TEXT_CHARS} characters: "
                + ", ".join(f"#{index}" for index in oversized[:5])
            )
            return []
        return [item for item in value if item.strip()]

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be a non-empty string")
    elif len(summary) > MAX_TEXT_CHARS:
        problems.append(f"summary must be at most {MAX_TEXT_CHARS} characters")

    issues: list[dict[str, Any]] = []
    raw_issues = data["issues"]
    if not isinstance(raw_issues, list):
        problems.append("issues must be an array")
    elif len(raw_issues) > MAX_LIST_ITEMS:
        problems.append(f"issues must hold at most {MAX_LIST_ITEMS} entries")
    else:
        for index, entry in enumerate(raw_issues):
            if not isinstance(entry, dict) or "description" not in entry:
                problems.append(f"issue #{index + 1} must be an object with 'description'")
                continue
            # The live NVIDIA model names a moment with the singular aliases
            # `frame` / `seconds` as often as the canonical `at_frame` /
            # `at_seconds`; normalize them before the shape check so a real
            # verdict is not thrown away for a naming variant. A canonical
            # key already present wins over an alias. The normalization runs
            # on a copy, so the caller's model payload keeps the names the
            # model actually wrote.
            normalized = dict(entry)
            if "frame" in normalized:
                normalized.setdefault("at_frame", normalized.pop("frame"))
            if "seconds" in normalized:
                normalized.setdefault("at_seconds", normalized.pop("seconds"))
            # Start/end pairs: {"start_frame": 9, "end_frame": 11} is the
            # same moment as {"at_frame": [9, 11]}. A lone half names a
            # boundary with no other, and dropping it would silently lose
            # where the model pointed, so it is refused instead.
            marked = len(problems)
            for start_key, end_key, canonical in (
                ("start_frame", "end_frame", "at_frame"),
                ("start_seconds", "end_seconds", "at_seconds"),
            ):
                start = normalized.pop(start_key, None)
                end = normalized.pop(end_key, None)
                if start is not None and end is not None:
                    normalized.setdefault(canonical, [start, end])
                elif start is not None or end is not None:
                    problems.append(f"issue #{index + 1} needs {start_key} and {end_key} together")
            if len(problems) > marked:
                continue
            unexpected = sorted(set(normalized) - {"description", "at_seconds", "at_frame"})
            if unexpected:
                problems.append(
                    f"issue #{index + 1} has unexpected key(s): {', '.join(unexpected)}"
                )
                continue
            description = normalized["description"]
            if not isinstance(description, str) or not description.strip():
                problems.append(f"issue #{index + 1} needs a non-empty description")
                continue
            if len(description) > MAX_TEXT_CHARS:
                problems.append(
                    f"issue #{index + 1} description must be at most {MAX_TEXT_CHARS} characters"
                )
                continue
            issue: dict[str, Any] = {"description": description.strip()}
            # Both moments are the same check over one key: the same
            # single-value-or-pair rule, the same refusal for a present value
            # that is neither. `at_frame` additionally refuses a negative
            # index, which has no meaning in a frame list.
            rejected = False
            for key, non_negative, expectation in (
                ("at_seconds", False, "[start, end] numbers with start <= end, or a single second"),
                (
                    "at_frame",
                    True,
                    "[start, end] non-negative numbers with start <= end, or a single frame index",
                ),
            ):
                moment = _moment(normalized.get(key), non_negative=non_negative)
                if normalized.get(key) is not None and moment is None:
                    problems.append(f"issue #{index + 1} {key} must be {expectation}")
                    rejected = True
                    break
                if moment is not None:
                    issue[key] = moment
            if rejected:
                continue
            issues.append(issue)

    known = {item.key for item in BASE_RUBRIC}
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

    # The string-list fields validate here too, before the gate below: a call
    # placed after it would append a problem nobody reads and return an
    # empty list in place of the model's malformed answer.
    strengths = strings("strengths")
    recommended_changes = strings("recommended_changes")
    limitations = strings("limitations")

    if problems:
        raise DeadeyeError(
            f"{origin} returned an invalid structure (schema mismatch): " + "; ".join(problems)
        )
    return {
        "summary": summary.strip(),
        "strengths": strengths,
        "issues": issues,
        "recommended_changes": recommended_changes,
        "rubric_scores": scores,
        "confidence": round(float(confidence), 4),
        "limitations": limitations,
    }
