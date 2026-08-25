"""The hash-addressed evidence envelope one review produces.

The envelope is the machine contract of a `deadeye review`: it names every
file actually submitted (by SHA-256), the sampling that decided the set, the
provider and model, the rubric and prompt versions, the validated result, and
the disclosure that preceded the upload — with credentials absent by
construction and vendor payload redacted. Consuming tools (`shamway
review-video`, `review_video.py`) embed this envelope in their own evidence
documents, which add the fields only they know (generation parameters, suite
and case).

A later review never overwrites an earlier envelope by default: both remain,
hash-addressed, so revisions stay comparable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._version import __version__
from .errors import DeadeyeError
from .intent import INTENT_SCHEMA_VERSION, ReviewIntent, redact
from .result import ADVISORY_NOTE, PROMPT_VERSION, RUBRIC_VERSION
from .sampling import SamplingRecord

EVIDENCE_SCHEMA_VERSION = 1

# A provider's usage block reports its cost through names like
# `totalTokenCount`, so it cannot reuse intent.SENSITIVE_KEY_PARTS wholesale:
# there "token" is billing, not authentication. It keeps every count and
# still drops the names a secret actually travels in.
USAGE_SENSITIVE_KEY_PARTS = tuple(
    part
    for part in ("api_key", "apikey", "authorization", "credential", "password", "secret", "token")
    if part != "token"
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def build_envelope(
    *,
    media_entries: tuple[dict[str, Any], ...],
    sampling: SamplingRecord,
    intent: ReviewIntent,
    intent_raw: bytes,
    provider_name: str,
    endpoint_mode: str,
    model_requested: str,
    model_reported: str | None,
    prompt: str,
    result: dict[str, Any] | None,
    error: str | None,
    raw_response: str | None,
    usage: dict[str, Any] | None,
    total_bytes: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    """The full machine-readable record of one review."""
    return {
        "kind": "deadeye-review",
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "tool_version": __version__,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "review_id": uuid.uuid4().hex,
        "advisory_only": True,
        "note": ADVISORY_NOTE,
        "intent": {
            "sha256": sha256_bytes(intent_raw),
            "schema_version": INTENT_SCHEMA_VERSION,
            "content": intent.as_dict(),
        },
        "media": media_entries,
        "sampling": {
            "frames_available": sampling.frames_available,
            "frames_submitted": sampling.frames_submitted,
            "sampled": sampling.sampled,
            "note": sampling.note,
        },
        "provider": {
            "name": provider_name,
            "endpoint_mode": endpoint_mode,
            "model_requested": model_requested,
            "model_reported": model_reported,
        },
        "rubric_version": RUBRIC_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt": prompt,
        "result": result,
        "error": error,
        # Raw responses are opt-in and redacted; they carry debugging value and
        # sometimes the provider's own request metadata. Usage is redacted for
        # the same reason: it is vendor payload, and nothing a provider sent
        # may reach stdout, JSON output, or evidence without the backstop.
        "raw_provider_response": raw_response,
        "usage": (
            redact(dict(usage), USAGE_SENSITIVE_KEY_PARTS)
            if usage
            else {"reported_by_provider": False}
        ),
        "disclosure": {
            "network_consent": True,
            "third_party": provider_name,
            "file_count": len(media_entries),
            "total_bytes": total_bytes,
        },
        "parameters": redact(params),
    }


def write_evidence(path: Path, document: dict[str, Any], *, force: bool) -> tuple[Path, str]:
    """Write an envelope atomically; refuse to overwrite an earlier one."""
    if path.is_file() and not force:
        raise DeadeyeError(
            f"{path} already holds an earlier review and a later review never "
            "overwrites one by default; compare the documents, or pass --force"
        )
    payload = json.dumps(document, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, payload)
    return path, sha256_bytes(payload.encode("utf-8"))


def _atomic_write(path: Path, payload: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        # Bytes, never text mode: the digest returned for this payload hashes
        # its LF-encoded UTF-8 exactly, and a text-mode write would let the
        # platform's newline translation rewrite it on disk (CRLF), making
        # every stored evidence hash disagree with its own file.
        temporary.write_bytes(payload.encode("utf-8"))
        temporary.replace(path)
    except OSError:
        # A failed or interrupted write must not strand a partial file that
        # looks like evidence beside the real one; surface the original fault.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
