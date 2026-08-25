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
    return value.strip()


def _string_list(data: dict[str, Any], key: str, origin: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeadeyeError(f"{origin}: field {key!r} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


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
    if version != INTENT_SCHEMA_VERSION:
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

    references: list[ReferenceMedia] = []
    raw_references = data.get("references")
    if raw_references is not None:
        if not isinstance(raw_references, list):
            raise DeadeyeError(f"{origin}: 'references' must be a list")
        for index, entry in enumerate(raw_references):
            label = f"{origin}: reference #{index + 1}"
            if not isinstance(entry, dict) or set(entry) != {"path", "purpose"}:
                raise DeadeyeError(f"{label}: each reference needs exactly 'path' and 'purpose'")
            reference_path = entry["path"]
            reference_purpose = entry["purpose"]
            if not isinstance(reference_path, str) or not reference_path:
                raise DeadeyeError(f"{label}: 'path' must be a non-empty string")
            if not isinstance(reference_purpose, str) or not reference_purpose.strip():
                raise DeadeyeError(f"{label}: 'purpose' must state what the comparison is for")
            references.append(
                ReferenceMedia(path=Path(reference_path), purpose=reference_purpose.strip())
            )

    return ReviewIntent(
        purpose=purpose,
        subject=_string_field(data, "subject", origin),
        camera_path=camera_path,
        desired_qualities=_string_field(data, "desired_qualities", origin),
        avoid=_string_list(data, "avoid", origin),
        references=tuple(references),
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
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadeyeError(f"{origin} is not valid JSON: {exc}") from exc


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
