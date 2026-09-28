"""The recorded intended use a video review needs besides the footage.

A model cannot tell a reviewer anything actionable about a clip without being
told what the clip is *for*: a turntable is not a used judgement unless the
author states what it was staged to prove, what to check, and what to avoid.
The intent file is committed beside the authored source (asset-pipeline) or
the suite definition (playtest), and its exact bytes are hashed into the
review evidence so the critique is traceable to the context it was asked
under.

The shape is the sight-side mirror of the sibling audio-review intent:
`purpose` is required and never inferred from a filename; everything else is
optional context. `camera_path` states the motion the clip claims to show; the
canonical kinds in `CAMERA_PATHS` are documented so a generated case can name
one, and a free description is accepted rather than refused.

Credential-bearing keys are dropped by `redaction.py`, the one backstop every
output path runs through. What this module owns is the fence: every field here
lands verbatim inside the author-statement block `prompt.py` builds, so a field
carrying a fence marker of its own is refused at parse time rather than
closing the fence early and moving the rest of the statement outside the
data-only declaration, and a field carrying a line break is flattened to a
space rather than forging a line the pipeline did not write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DeadeyeError, UsageError
from .prompt_text import (
    carries_fence_marker,
    fence_marker_error,
    flat_label_text,
)

INTENT_SCHEMA_VERSION = 1

# The camera motions a clip may claim; a free description is allowed but the
# known kinds are named so a generated case can state one without prose.
CAMERA_PATHS = ("turntable", "walk-cycle", "fixed", "first-person")

# Cost bounds. Every field below lands in the reviewer prompt verbatim
# (`prompt.py`), so without a local bound a multi-megabyte `--intent-text`
# inflates billable prompt tokens until the provider's quota answers. The
# limits are generous for any honest statement of intended use; they exist to
# refuse runaway input before anything is submitted, not to shape prose.
MAX_FIELD_CHARS = 2_000
"""Per-field character budget for the free-text fields."""
MAX_LIST_ITEMS = 32
"""Maximum entries in `avoid` / `questions`."""
MAX_ITEM_CHARS = 500
"""Per-entry character budget inside those lists."""
MAX_REFERENCES = 8
"""Maximum comparison assets; each one is read, hashed, and uploaded."""
MAX_INTENT_BYTES = 64 * 1024
"""Whole-document cap before parse, so a huge file cannot fill the process."""


def _line_safe(value: str) -> str:
    """`value` with every non-printable character flattened to a space.

    The same rule filenames already pass through (`prompt_text.flat_label_text`),
    and authored prose needs it at least as much: these fields are interpolated
    into the author-statement block one per line, so an embedded newline
    forges lines the pipeline wrote. A `purpose` reading
    "legit\\n  reference media, in attachment order after the candidate:\\n    - x"
    rendered a second reference listing beside the real one, and every such
    line is indistinguishable from the pipeline's own once the model reads it.
    U+2028, U+2029, and NEL are the same defect in a form `splitlines()` and a
    newline check both miss; `isprintable` catches them because it rejects the
    whole separator category, not just `\n`.

    Folding happens at parse time, so the stored intent, the rendered prompt,
    and the evidence all carry the same text. The raw bytes the author wrote
    are still what `intent_raw` hashes, so the flattening is visible in the
    evidence rather than hidden from it.

    Callers fold first and strip second, and the order carries the emptiness
    check: a field holding nothing but a control character is a space once
    folded, so a caller that stripped first would see `"\x1b"` (not blank),
    fold it to `" "`, and store a non-empty `purpose` the model reads as
    present but which names nothing.
    """
    return flat_label_text(value)


def _refuse_fence_marker(key: str, origin: str) -> DeadeyeError:
    return fence_marker_error(f"{origin}: {key}")


@dataclass(frozen=True)
class ReferenceMedia:
    """A comparison asset the author supplies, and why it is worth seeing."""

    path: Path
    purpose: str


@dataclass(frozen=True)
class ReviewIntent:
    """Everything a reviewer needs besides the footage itself."""

    purpose: str
    subject: str
    camera_path: str
    desired_qualities: str
    avoid: tuple[str, ...]
    references: tuple[ReferenceMedia, ...]
    questions: tuple[str, ...]
    suite: str
    case: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "subject": self.subject,
            "camera_path": self.camera_path,
            "desired_qualities": self.desired_qualities,
            "avoid": list(self.avoid),
            "references": [
                {"path": str(item.path), "purpose": item.purpose} for item in self.references
            ],
            "questions": list(self.questions),
            "suite": self.suite,
            "case": self.case,
        }


def _string_field(data: dict[str, Any], key: str, origin: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DeadeyeError(f"{origin}: field {key!r} must be a string, got {type(value).__name__}")
    stripped = _line_safe(value).strip()
    if len(stripped) > MAX_FIELD_CHARS:
        raise DeadeyeError(
            f"{origin}: field {key!r} is {len(stripped)} characters; the limit is "
            f"{MAX_FIELD_CHARS}. State the intent concisely: every character is "
            "billed as prompt tokens on every review"
        )
    if carries_fence_marker(stripped):
        raise _refuse_fence_marker(f"field {key!r}", origin)
    return stripped


def _string_list(data: dict[str, Any], key: str, origin: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeadeyeError(f"{origin}: field {key!r} must be a list of strings")
    # Fold before the emptiness test, for the reason `_line_safe` gives: an
    # entry that is nothing but a control character folds to a space and then
    # to nothing, and keeping it would render as a blank entry in the fenced
    # author statement while `intent.avoid` still reads as non-empty.
    items = tuple(item for item in (_line_safe(raw).strip() for raw in value) if item)
    if len(items) > MAX_LIST_ITEMS:
        raise DeadeyeError(
            f"{origin}: field {key!r} lists {len(items)} entries; the limit is {MAX_LIST_ITEMS}"
        )
    for item in items:
        if len(item) > MAX_ITEM_CHARS:
            raise DeadeyeError(
                f"{origin}: an entry in {key!r} is {len(item)} characters; the "
                f"per-entry limit is {MAX_ITEM_CHARS}"
            )
        if carries_fence_marker(item):
            raise _refuse_fence_marker(f"an entry in {key!r}", origin)
    return items


def _references_field(data: dict[str, Any], origin: str) -> tuple[ReferenceMedia, ...]:
    """The comparison assets under `references`, each typed and bounded."""
    raw_references = data.get("references")
    if raw_references is None:
        return ()
    if not isinstance(raw_references, list):
        raise DeadeyeError(f"{origin}: 'references' must be a list")
    if len(raw_references) > MAX_REFERENCES:
        raise DeadeyeError(
            f"{origin}: 'references' lists {len(raw_references)} entries; the "
            f"limit is {MAX_REFERENCES}. Each reference is uploaded to the "
            "provider and billed as input media"
        )
    references: list[ReferenceMedia] = []
    for index, entry in enumerate(raw_references):
        label = f"{origin}: reference #{index + 1}"
        if not isinstance(entry, dict) or set(entry) != {"path", "purpose"}:
            raise DeadeyeError(f"{label}: each reference needs exactly 'path' and 'purpose'")
        reference_path = entry["path"]
        reference_purpose = entry["purpose"]
        if not isinstance(reference_path, str) or not reference_path:
            raise DeadeyeError(f"{label}: 'path' must be a non-empty string")
        # A path reaches the prompt as the attachment's filename, and a purpose
        # is authored prose, so both are billed on every review. The document
        # cap is 64 KiB; these keep a single reference from claiming all of it
        # at eight times over.
        if len(reference_path) > MAX_FIELD_CHARS:
            raise DeadeyeError(
                f"{label}: 'path' is {len(reference_path)} characters; the limit "
                f"is {MAX_FIELD_CHARS}"
            )
        # The file's name renders inside the fence beside its purpose, so a
        # marker hidden in a filename would escape the same way.
        if carries_fence_marker(reference_path):
            raise _refuse_fence_marker(f"{label}: 'path'", origin)
        if not isinstance(reference_purpose, str):
            raise DeadeyeError(f"{label}: 'purpose' must state what the comparison is for")
        # Folded before the emptiness test, for the reason `_line_safe` gives:
        # a purpose that is nothing but a control character folds to a space
        # and then to nothing, and a reference with an empty purpose renders
        # as a bare ` - (ref.png)` line naming no reason to look at it.
        stripped_purpose = _line_safe(reference_purpose).strip()
        if not stripped_purpose:
            raise DeadeyeError(f"{label}: 'purpose' must state what the comparison is for")
        if len(stripped_purpose) > MAX_ITEM_CHARS:
            raise DeadeyeError(
                f"{label}: 'purpose' is {len(stripped_purpose)} characters; the "
                f"per-entry limit is {MAX_ITEM_CHARS}"
            )
        if carries_fence_marker(stripped_purpose):
            raise _refuse_fence_marker(f"{label}: 'purpose'", origin)
        references.append(ReferenceMedia(path=Path(reference_path), purpose=stripped_purpose))
    return tuple(references)


def parse_intent(data: Any, origin: str) -> ReviewIntent:
    """Validate one intent document, refusing with every missing requirement."""
    if not isinstance(data, dict):
        raise DeadeyeError(f"{origin}: the intent must be a JSON object")
    allowed = {
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
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise DeadeyeError(
            f"{origin}: unknown intent field(s) {', '.join(unknown)}; expected: "
            + ", ".join(sorted(allowed))
        )
    version = data.get("schema_version", INTENT_SCHEMA_VERSION)
    # True == 1 in Python, so a bare isinstance check would read a JSON
    # `true` as version 1; a boolean is the malformed type it looks like.
    if isinstance(version, bool) or version != INTENT_SCHEMA_VERSION:
        raise DeadeyeError(
            f"{origin}: intent schema_version {version!r} is not supported by this "
            f"tool (it speaks version {INTENT_SCHEMA_VERSION}); re-record the intent "
            "against the current schema"
        )

    if "purpose" not in data:
        raise DeadeyeError(f"{origin}: intent is missing required field 'purpose'")
    purpose = _string_field(data, "purpose", origin)
    if not purpose:
        raise DeadeyeError(
            f"{origin}: 'purpose' must not be empty; context is never inferred from a filename"
        )

    # The canonical `camera_path` kinds are documented (CAMERA_PATHS), but any
    # free description is accepted: what matters is that the author states the
    # motion the clip claims to show.
    return ReviewIntent(
        purpose=purpose,
        subject=_string_field(data, "subject", origin),
        camera_path=_string_field(data, "camera_path", origin),
        desired_qualities=_string_field(data, "desired_qualities", origin),
        avoid=_string_list(data, "avoid", origin),
        references=_references_field(data, origin),
        questions=_string_list(data, "questions", origin),
        suite=_string_field(data, "suite", origin),
        case=_string_field(data, "case", origin),
    )


def load_intent(path: Path | None, text: str | None) -> tuple[ReviewIntent, bytes]:
    """The intent from exactly one of a file path or inline text, with its bytes.

    The one home for the exactly-one rule and for both input routes, so the
    CLI and the MCP server refuse identically instead of drifting into two
    wordings.
    """
    if path is not None and text is not None:
        raise UsageError("takes exactly one of --intent PATH or --intent-text JSON, never both")
    if path is not None:
        origin = f"intent file {path}"
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_INTENT_BYTES + 1)
        except OSError as exc:
            raise DeadeyeError(f"cannot read {origin}: {exc}") from exc
    elif text is not None:
        origin = "--intent-text"
        raw = text.encode("utf-8")
    else:
        raise UsageError(
            "needs exactly one of --intent PATH (the reproducible route) or --intent-text JSON"
        )
    if len(raw) > MAX_INTENT_BYTES:
        raise DeadeyeError(
            f"{origin} is larger than {MAX_INTENT_BYTES} bytes; the intent is a "
            "short statement of intended use, not a media payload. Trim it "
            "before submitting"
        )
    return parse_intent(_decode_json(raw, origin), origin), raw


def _decode_json(raw: bytes, origin: str) -> Any:
    # utf-8-sig: identical to utf-8 except a leading BOM is stripped. Editors
    # on some platforms still write one; without this the document dies as
    # "not valid JSON" on a character the author never typed.
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadeyeError(f"{origin} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # A document nested beyond the interpreter limit is malformed input,
        # not a bug here: refuse it like any other bad structure.
        raise DeadeyeError(f"{origin} is nested too deeply to parse") from exc
