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
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._version import __version__
from .errors import DeadeyeError
from .intent import INTENT_SCHEMA_VERSION, SENSITIVE_KEY_PARTS, ReviewIntent, redact
from .result import ADVISORY_NOTE, PROMPT_VERSION, RUBRIC_VERSION
from .sampling import SamplingRecord

EVIDENCE_SCHEMA_VERSION = 1

# A provider's usage block reports its cost through names like
# `totalTokenCount`, so it cannot reuse intent.SENSITIVE_KEY_PARTS wholesale:
# there "token" is billing, not authentication. It keeps every count and
# still drops the names a secret actually travels in. Derived from the
# canonical tuple, minus that one documented exception, so the two lists
# cannot drift apart.
USAGE_SENSITIVE_KEY_PARTS = tuple(part for part in SENSITIVE_KEY_PARTS if part != "token")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> tuple[str, int, bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DeadeyeError(f"cannot hash file {path}: {exc}") from exc
    # The caller must retain the exact bytes for the inline-media request.
    # Reading into chunks and joining them after hashing kept both the chunk
    # list and a second whole-file copy alive at once. Hash the one retained
    # buffer directly instead.
    return sha256_bytes(payload), len(payload), payload


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
    elapsed_seconds: float,
) -> dict[str, Any]:
    """The full machine-readable record of one review."""
    return {
        "kind": "deadeye-review",
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "tool_version": __version__,
        # Instant, not local wall time: a zone-less stamp would be read in
        # the consumer's TZ (a late-evening run becoming the previous day
        # in US zones) and a host-local offset would change meaning on
        # another machine. `datetime.now(UTC)` is TZ-independent; isoformat
        # on that aware value always carries `+00:00`.
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
            # Monotonic seconds the submission took (`time.perf_counter` in
            # review.py). A wall-clock delta would go negative or jump on an
            # NTP step mid-call; latency is part of a call's record just
            # like token counts, and neither is estimated when absent.
            "elapsed_seconds": round(elapsed_seconds, 3),
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


def ensure_writable(path: Path, *, force: bool) -> None:
    """Refuse an occupied evidence path before anything is contacted.

    The one home for the overwrite predicate and its wording: `run_review`
    calls it as a pre-flight check (a rerun into an existing path is refused
    before credentials are read or any byte leaves the machine, so the guard
    never has to be paid for), and `write_evidence` re-checks at write time.
    """
    if path.exists() and not path.is_file():
        raise DeadeyeError(f"{path} is not a regular file and cannot hold review evidence")
    if (path.is_file() or path.is_symlink()) and not force:
        raise DeadeyeError(
            f"{path} already holds an earlier review and a later review never "
            "overwrites one by default; compare the documents, or pass --force"
        )


def write_evidence(path: Path, document: dict[str, Any], *, force: bool) -> tuple[Path, str]:
    """Write an envelope atomically; refuse to overwrite an earlier one."""
    ensure_writable(path, force=force)
    payload = json.dumps(document, indent=2, sort_keys=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, payload)
    except OSError as exc:
        # A bare errno would leave the caller guessing which argument failed;
        # name the evidence path the way every other refusal names its cause.
        raise DeadeyeError(f"cannot write evidence file {path}: {exc}") from exc
    return path, sha256_bytes(payload.encode("utf-8"))


def _atomic_write(path: Path, payload: str) -> None:
    temporary: Path | None = None
    try:
        # `NamedTemporaryFile` creates a unique file with private permissions
        # in the destination directory. A predictable `path + ".tmp"` name
        # would let another user who can write that directory pre-create a
        # symlink and redirect this write before the final replace.
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            # Bytes, never text mode: the digest returned for this payload
            # hashes its LF-encoded UTF-8 exactly, and a text-mode write would
            # let the platform's newline translation rewrite it on disk
            # (CRLF), making every stored evidence hash disagree with its own
            # file.
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        # Any exit except a successful replace (OSError, KeyboardInterrupt,
        # a failed flush) must not strand a partial file that looks like
        # evidence beside the real one. After replace the `.tmp` name is
        # gone, so `temporary` is cleared first.
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
