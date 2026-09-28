"""The hash-addressed evidence envelope one review produces.

The envelope is the machine contract of a `deadeye review`: it names every
file actually submitted (by SHA-256), the sampling that decided the set, the
provider and model, the generation parameters the request carried, the rubric
and prompt versions, the validated result, and the disclosure that preceded
the upload — with credentials absent by construction and vendor payload
redacted. Consuming tools (`shamway review-video`, `review_video.py`) embed
this envelope in their own evidence documents, which add the fields only they
know (suite and case).

A later review never overwrites an earlier envelope by default: both remain,
hash-addressed, so revisions stay comparable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._version import __version__
from .errors import DeadeyeError
from .intent import INTENT_SCHEMA_VERSION, ReviewIntent
from .prompt import PROMPT_VERSION
from .redaction import redact
from .result import ADVISORY_NOTE, RUBRIC_VERSION
from .sampling import SamplingRecord

EVIDENCE_SCHEMA_VERSION = 1

# A provider's usage block reports its cost through names like
# `totalTokenCount`, so the usage path cannot simply drop every token-shaped
# key. Dropping the `token` part wholesale instead is what let
# `access_token`, `id_token`, and `refresh_token` through the one backstop
# every output path runs through, into stored evidence, stdout, and MCP
# payloads. These are the billing names that must survive; every other
# token-shaped key is treated as the credential it usually is.
USAGE_BILLING_KEY_PARTS = (
    "cachetokencount",
    "cachedcontenttokencount",
    "candidatestokencount",
    "prompttokencount",
    "thoughtstokencount",
    "totaltokencount",
    "completion_tokens",
    "prompt_tokens",
    "total_tokens",
)


def sha256_bytes(payload: bytes) -> str:
    """The SHA-256 hex digest of `payload`."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> tuple[str, int, bytes]:
    """Read a submitted file once, returning (sha256 hex, byte length, bytes).

    The bytes come back with the digest because every adapter inlines them in
    the request; the caller keeps the one buffer the digest was taken from
    instead of re-reading the file (which may have changed) before submitting.
    """
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DeadeyeError(f"cannot hash file {path}: {exc}") from exc
    # One retained buffer, hashed directly: a chunked read that joined into a
    # second whole-file copy kept both alive at once.
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
    generation: dict[str, Any],
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
            # The map between the two index spaces an `at_frame` has to cross:
            # the model counts the attachments it was shown, this names each of
            # them in the clip's own frame order, so a consumer reads
            # `submitted_frame_indices[at_frame]` for the frame a critique
            # points at. Empty for a video submission.
            "submitted_frame_indices": list(sampling.frame_indices),
            "note": sampling.note,
        },
        "provider": {
            "name": provider_name,
            "endpoint_mode": endpoint_mode,
            "model_requested": model_requested,
            "model_reported": model_reported,
            # The sampling parameters the submission actually carried, read
            # from the same home the request body was built from. A review is
            # traceable to its prompt, its bytes, and its model; the
            # temperature and output cap it was generated at are the fourth
            # thing that decides what came back, and an envelope that named
            # three of them left two runs of the same clip indistinguishable.
            "generation": generation,
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
            redact(dict(usage), exceptions=USAGE_BILLING_KEY_PARTS)
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


# An exclusive reserve is empty by construction: a real envelope always
# serializes to bytes, so a zero-byte destination is either a placeholder a
# writer has not replaced yet or one a SIGKILL stranded between reserve and
# replace. The window a live writer needs is sub-second (the payload is
# already fsync'd in its temporary file before the reserve), so a placeholder
# this old belongs to a run that died. Reclaiming it is what keeps a crash
# from wedging the evidence path forever with a refusal that claims an
# earlier review where no review was ever published.
_STALE_PLACEHOLDER_SECONDS = 60.0

# The age of an on-disk file is only readable from the wall clock, so the
# reclaim must measure it there. The wall clock can step while this process
# runs (NTP, a manual `date -s`, a VM restored from a snapshot), and a forward
# step makes a placeholder a live writer reserved a moment ago read as hours
# old: the reclaim then unlinks that writer's reservation and both writers
# publish, which is the one outcome the exclusive reserve exists to prevent.
#
# The process's own elapsed time is monotonic, so `wall_at_start +
# (monotonic() - monotonic_at_start)` moves only as fast as real time has
# passed since this module was imported. Taking the smaller of the two clocks
# bounds `now` from below, so a step can only make a placeholder read fresher
# than it is and the run refuses, never that a live writer's name is freed.
# CLOCK_MONOTONIC stops during suspend, which pushes the bound further back
# and fails the same way.
_WALL_AT_IMPORT = time.time()
_MONOTONIC_AT_IMPORT = time.monotonic()


def _now_floor() -> float:
    """A wall-clock reading that never runs ahead of this process's own time."""
    return min(
        time.time(),
        _WALL_AT_IMPORT + (time.monotonic() - _MONOTONIC_AT_IMPORT),
    )


def _occupied_evidence_message(path: Path) -> str:
    return (
        f"{path} already holds an earlier review and a later review never "
        "overwrites one by default; compare the documents, or pass --force"
    )


def _pending_write_message(path: Path) -> str:
    return (
        f"{path} is occupied by a review write in progress; it holds no "
        "published review, so the next run takes it once that write finishes"
    )


def _stale_placeholder_stat(path: Path) -> os.stat_result | None:
    """The stat of `path` when it is an empty reserve old enough to reclaim.

    Returning the stat rather than a bool is what lets the reclaim act on the
    file it inspected: a name can stop being that file between the check and
    the unlink.
    """
    try:
        occupied = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(occupied.st_mode) or occupied.st_size != 0:
        return None
    if (_now_floor() - occupied.st_mtime) < _STALE_PLACEHOLDER_SECONDS:
        return None
    return occupied


def _is_abandoned_placeholder(path: Path) -> bool:
    """Whether `path` is an empty reserve old enough to reclaim, not one in flight."""
    return _stale_placeholder_stat(path) is not None


# One file's identity: device and inode. A name is not an identity, and the
# evidence path is shared state across processes, so every destructive act on
# a name another writer may hold is fenced by it.
_FileIdentity = tuple[int, int]


def _identity_of(status: os.stat_result) -> _FileIdentity:
    return (status.st_dev, status.st_ino)


def _fsync_directory(directory: Path) -> None:
    """Make a rename into `directory` durable, not just the file it renamed.

    `fsync` on the payload before the replace fixes the bytes; it says nothing
    about the name. A crash between `replace` returning and the journal
    committing the directory entry can leave the destination holding what it
    held before, which here is the empty reserve: the caller has already been
    handed the envelope and its digest, and the next run finds a zero-byte
    placeholder and waits out the reclaim instead. A `--force` overwrite is
    worse, silently reverting to the envelope it replaced.

    A platform with no `O_DIRECTORY` has no directory handle to sync, so
    there is nothing to do. Where the handle exists, a failing `fsync` means
    the publish is not durable and the caller is told, rather than handed a
    digest for a file a power cut may take back.
    """
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    fd = os.open(directory, os.O_RDONLY | directory_flag)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_reserve(path: Path, flags: int) -> _FileIdentity:
    """Create the exclusive placeholder and return the identity of the file
    this call created (so a later cleanup can tell it from its successor)."""
    fd = os.open(path, flags, 0o600)
    try:
        return _identity_of(os.fstat(fd))
    finally:
        os.close(fd)


def _name_holds(path: Path, identity: _FileIdentity) -> bool:
    """Whether `path` still names the file `identity` describes."""
    try:
        return _identity_of(path.stat()) == identity
    except OSError:
        return False


def _unlink_if_same_file(path: Path, identity: _FileIdentity) -> bool:
    """Unlink `path` only while it still names the file `identity` describes.

    `stat` then `unlink` is a check-then-act over a name another process owns.
    The window is one syscall wide, which is as narrow as POSIX allows
    (unlinking by inode does not exist), and it is the difference between
    clearing this writer's own placeholder and destroying a review another
    writer published into the same name.
    """
    if not _name_holds(path, identity):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def ensure_writable(path: Path, *, force: bool) -> None:
    """Refuse an occupied evidence path before anything is contacted.

    The wording lives with the exclusive publish in `_reserve_exclusive`:
    `run_review` calls this as a pre-flight check (a rerun into an existing
    path is refused before credentials are read or any byte leaves the
    machine, so the guard never has to be paid for), and `write_evidence`
    re-checks at write time. The preflight is not the lock; two writers can
    both see a free path, so the write itself occupies the name with
    `O_CREAT|O_EXCL` before replace.

    An empty destination is not a published review: it is a placeholder. A
    placeholder a live writer still holds is refused like any other occupied
    path (still for free, before any submission); one stranded by a crash is
    reclaimed, and the recovery run converges on the same path.

    A destination that cannot be written at all is refused here for the same
    reason: `write_evidence` creates the parents it needs, so the directory
    that decides the write is the nearest one that already exists, and
    checking it costs a `stat`. Without the check an unwritable directory is
    discovered only after the submission was billed, which is exactly the
    cost the preflight exists to avoid.
    """
    _ensure_writable_directory(path.parent)
    if path.exists() and not path.is_file():
        raise DeadeyeError(f"{path} is not a regular file and cannot hold review evidence")
    if (path.is_file() or path.is_symlink()) and not force:
        if not _is_empty(path):
            raise DeadeyeError(_occupied_evidence_message(path))
        if not _is_abandoned_placeholder(path):
            raise DeadeyeError(_pending_write_message(path))


def _ensure_writable_directory(directory: Path) -> None:
    """Refuse a destination whose nearest existing parent cannot be written to.

    `write_evidence` creates missing parents, so the write lands in the
    closest ancestor that is already there. That ancestor is the one to check,
    and it is checked for what the write needs: a directory to create the
    entry in, and permission to do it. The check is advisory; a permission
    that changes after it still lands on the write-time refusal.
    """
    ancestor = directory
    while not ancestor.exists():
        parent = ancestor.parent
        if parent == ancestor:
            return
        ancestor = parent
    if not ancestor.is_dir():
        raise DeadeyeError(f"{ancestor} is not a directory and cannot hold review evidence")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise DeadeyeError(f"{ancestor} is not writable and cannot hold review evidence")


def _is_empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        # Unreadable (permissions, a broken symlink): fail closed and let the
        # write-time reserve make the real decision.
        return False


def _refusal_for_occupant(path: Path) -> str:
    """Why an occupied name cannot be written: a live write, or a review."""
    return _pending_write_message(path) if _is_empty(path) else _occupied_evidence_message(path)


def write_evidence(path: Path, document: dict[str, Any], *, force: bool) -> tuple[Path, str]:
    """Write an envelope atomically; refuse to overwrite an earlier one."""
    ensure_writable(path, force=force)
    # Encoded once: the bytes written and the bytes the digest covers are the
    # same buffer, and an envelope carrying a preserved raw response is
    # megabytes this would otherwise encode twice.
    payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, payload, force=force)
    except OSError as exc:
        # A bare errno would leave the caller guessing which argument failed;
        # name the evidence path the way every other refusal names its cause.
        raise DeadeyeError(f"cannot write evidence file {path}: {exc}") from exc
    return path, sha256_bytes(payload)


def _reserve_exclusive(path: Path) -> _FileIdentity:
    """Occupy `path` only if the name is free; the no-overwrite publish lock.

    `ensure_writable` is a cheap preflight. Two processes can both see a
    missing file, both submit, then both `replace` onto the same path and
    the first envelope is gone. `O_CREAT|O_EXCL` is the atomic that makes
    the second writer fail instead of clobbering the first.

    A crash between the reserve and the replace strands an empty placeholder,
    and the next run would then refuse a path that holds no review at all.
    An empty occupant is reclaimed once it is older than
    `_STALE_PLACEHOLDER_SECONDS`; a fresh one belongs to a live writer and is
    refused, so the concurrent-duplicate guarantee is unchanged.

    Returns the identity of the placeholder created, which the caller uses to
    confirm the publish and to clear exactly that file on its way out: a
    `--force` run takes no reservation and can replace this placeholder, so
    holding one is not by itself proof that the name is still this writer's.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        return _open_reserve(path, flags)
    except FileExistsError:
        pass
    stale = _stale_placeholder_stat(path)
    if stale is None:
        raise DeadeyeError(_refusal_for_occupant(path)) from None
    # Reclaim: drop the stranded name, then take it. A writer that reclaimed
    # the same placeholder first wins this race, and the loser sees its own
    # `O_EXCL` fail on the fresh placeholder the winner just created.
    #
    # The unlink is fenced by the identity just observed. Age alone is not
    # proof of death: a writer stalled past `_STALE_PLACEHOLDER_SECONDS`
    # between reserve and replace (a suspended process, a starved host) can
    # publish in the window between this stat and this unlink, and an unlink
    # by name would then delete a review that was never overwritten on
    # purpose. If the occupant is not the file inspected, the reclaim does
    # not happen and the refusal names what actually holds the name.
    if not _unlink_if_same_file(path, _identity_of(stale)):
        raise DeadeyeError(_refusal_for_occupant(path)) from None
    try:
        return _open_reserve(path, flags)
    except FileExistsError:
        # Whoever took the name in the window above may be another writer's
        # fresh placeholder or a `--force` run that published an envelope
        # there, so the refusal names what actually holds the name instead of
        # assuming a write in flight.
        raise DeadeyeError(_refusal_for_occupant(path)) from None


def _atomic_write(path: Path, payload: bytes, *, force: bool) -> None:
    temporary: Path | None = None
    placeholder: _FileIdentity | None = None
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
            # hashes exactly what lands on disk, and a text-mode write would
            # let the platform's newline translation rewrite it (CRLF), making
            # every stored evidence hash disagree with its own file.
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not force:
            # Reserve the destination name before replace so a concurrent
            # writer cannot publish onto the same path. `--force` skips
            # this: overwrite is then the caller's stated intent.
            placeholder = _reserve_exclusive(path)
            # The reserve is not a lock a `--force` writer observes: that
            # writer takes no reserve at all, so its replace can land on the
            # name this call is holding, between the reserve above and the
            # publish below. Publishing then overwrites an envelope nobody
            # chose to replace, and the force run is left holding a digest for
            # bytes the file no longer has. The publish is fenced by the same
            # identity the reclaim and the cleanup use, so this run refuses
            # instead. The window between the check and the replace is one
            # syscall wide, as `_unlink_if_same_file` already is: POSIX has no
            # compare-and-swap rename, so this narrows the race rather than
            # closing it, and it closes the whole schedule where the force
            # writer published before this one reached its own publish.
            if not _name_holds(path, placeholder):
                raise DeadeyeError(
                    f"{path} was published into by another writer between this "
                    "run's reservation and its write; this run refuses rather "
                    "than overwrite a review nobody asked it to replace"
                )
        temporary.replace(path)
        temporary = None
        placeholder = None
        # After both names are cleared, so a failing sync cannot make the
        # cleanup below unlink a published envelope.
        _fsync_directory(path.parent)
    finally:
        # Any exit except a successful replace (OSError, KeyboardInterrupt,
        # a failed flush) must not strand a partial file that looks like
        # evidence beside the real one. After replace the `.tmp` name is
        # gone, so `temporary` is cleared first.
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        # The exclusive placeholder occupies the name between reserve and
        # replace, and is dropped only when it is still the file this call
        # created. The identity is the check a size test cannot make: after
        # the replace the destination holds the envelope (a different
        # inode), and a `--force` writer that published into the reclaimed
        # name in between would have its review deleted by a stat-then-unlink
        # that saw the placeholder a moment earlier.
        if placeholder is not None:
            with contextlib.suppress(OSError):
                if _unlink_if_same_file(path, placeholder):
                    # The dropped reserve is the crash's only record, so the
                    # unlink has to reach the journal too: a name that comes
                    # back after a power cut holds an empty placeholder the
                    # next run must age out before it can publish.
                    _fsync_directory(path.parent)
