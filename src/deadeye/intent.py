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
optional context. `camera_path` is the motion the clip claims to show, not a
free-form description of the asset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DeadeyeError

INTENT_SCHEMA_VERSION = 1

# The camera motions a clip may claim; a free description is allowed but the
# known kinds are named so a generated case can state one without prose.
CAMERA_PATHS = ("turntable", "walk-cycle", "fixed", "first-person")

# Fields whose names look credential-bearing are dropped wherever they would
# otherwise land in stored evidence. Credentials are never accepted as
# arguments in the first place; this is the backstop for a caller that hands
# the API a document directly.
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

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

# The reviewer prompt fences every intent field between the BEGIN/END AUTHOR
# STATEMENT markers and declares that block data-only (`prompt.py`). A field
# carrying a marker line of its own could close that fence early and move
# everything after it outside the data-only declaration, so the markers are
# refused wherever intent text is accepted.
FENCE_MARKERS = ("-----BEGIN AUTHOR STATEMENT", "-----END AUTHOR STATEMENT")


def _carries_fence_marker(value: str) -> bool:
    return any(marker in value for marker in FENCE_MARKERS)


def _refuse_fence_marker(key: str, origin: str) -> DeadeyeError:
    return DeadeyeError(
        f"{origin}: {key} contains an author-statement fence marker "
        f"({' or '.join(FENCE_MARKERS)}); reword it without that line so the "
        "reviewer prompt's data-only fence cannot be escaped"
    )


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
    stripped = value.strip()
    if len(stripped) > MAX_FIELD_CHARS:
        raise DeadeyeError(
            f"{origin}: field {key!r} is {len(stripped)} characters; the limit is "
            f"{MAX_FIELD_CHARS}. State the intent concisely: every character is "
            "billed as prompt tokens on every review"
        )
    if _carries_fence_marker(stripped):
        raise _refuse_fence_marker(f"field {key!r}", origin)
    return stripped


def _string_list(data: dict[str, Any], key: str, origin: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeadeyeError(f"{origin}: field {key!r} must be a list of strings")
    items = tuple(item.strip() for item in value if item.strip())
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
        if _carries_fence_marker(item):
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
        # The file's name renders inside the fence beside its purpose, so a
        # marker hidden in a filename would escape the same way.
        if _carries_fence_marker(reference_path):
            raise _refuse_fence_marker(f"{label}: 'path'", origin)
        if not isinstance(reference_purpose, str) or not reference_purpose.strip():
            raise DeadeyeError(f"{label}: 'purpose' must state what the comparison is for")
        stripped_purpose = reference_purpose.strip()
        if _carries_fence_marker(stripped_purpose):
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

    camera_path = _string_field(data, "camera_path", origin)
    # The canonical kinds are documented, but any free description is accepted:
    # what matters is that the author states the motion the clip claims to show.

    return ReviewIntent(
        purpose=purpose,
        subject=_string_field(data, "subject", origin),
        camera_path=camera_path,
        desired_qualities=_string_field(data, "desired_qualities", origin),
        avoid=_string_list(data, "avoid", origin),
        references=_references_field(data, origin),
        questions=_string_list(data, "questions", origin),
        suite=_string_field(data, "suite", origin),
        case=_string_field(data, "case", origin),
    )


def load_intent_file(path: Path) -> tuple[ReviewIntent, bytes]:
    """Read and validate an intent file; return it with its exact bytes."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DeadeyeError(f"cannot read intent file {path}: {exc}") from exc
    return parse_intent(_decode_json(raw, f"intent file {path}"), f"intent file {path}"), raw


def parse_intent_text(text: str) -> tuple[ReviewIntent, bytes]:
    """Validate an inline intent document; return it with its exact bytes."""
    raw = text.encode("utf-8")
    return parse_intent(_decode_json(raw, "--intent-text"), "--intent-text"), raw


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


def redact(value: Any, parts: tuple[str, ...] = SENSITIVE_KEY_PARTS) -> Any:
    """Deep-copy a JSON-shaped value, dropping credential-bearing mapping keys."""
    if isinstance(value, dict):
        return {
            key: redact(item, parts)
            for key, item in value.items()
            if isinstance(key, str) and not _is_sensitive_key(key, parts)
        }
    if isinstance(value, list):
        return [redact(item, parts) for item in value]
    return value


def _is_sensitive_key(key: str, parts: tuple[str, ...] = SENSITIVE_KEY_PARTS) -> bool:
    lowered = key.lower()
    return lowered == "key" or any(part in lowered for part in parts)


def redact_json_text(text: str, parts: tuple[str, ...] = SENSITIVE_KEY_PARTS) -> str:
    """Redact credential-bearing keys from a JSON-encoded document string.

    A raw provider response arrives as one string, which plain `redact()`
    would return untouched however structured its contents are: the backstop
    walks mappings, and a string is a leaf. When the text parses as a JSON
    object or array, its mapping keys are redacted and the document
    re-serialized; anything else (model prose, a bare scalar, broken or
    truncated JSON) comes back byte-identical: there is nothing
    structure-shaped to clean, and guessing further would rewrite the record.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    return json.dumps(redact(parsed, parts))
