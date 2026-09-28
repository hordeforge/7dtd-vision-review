"""Clip discovery and frame sampling.

A clip is either a muxed video file or a directory holding a frame sequence
(`frame-XXXX.png`), optionally with the muxed mp4 and the capture's client log
beside it — exactly the shape `7dtd-playtest`'s `capture_video.sh` produces
and `shamway client capture --clip` adopts. Providers differ in what they can
ingest, so this module asks the adapter for its declared limit and samples
down (even spacing, always including the first and last frame) rather than
silently truncating from one end. When frames are dropped, the evidence
records how many and which sampling was used; a review that quietly saw only
the first eight frames of a forty-frame turntable is not honest about what it
actually judged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import DeadeyeError

# The one suffix -> MIME table; the accepted-suffix sets below are derived
# from it so the two can never drift apart.
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
IMAGE_SUFFIXES = tuple(
    suffix for suffix, mime in MIME_BY_SUFFIX.items() if mime.startswith("image/")
)
VIDEO_SUFFIXES = tuple(
    suffix for suffix, mime in MIME_BY_SUFFIX.items() if mime.startswith("video/")
)
LOG_SUFFIXES = (".log",)

_FRAME_RE = re.compile(r"^frame-(\d+)\.(?:png|jpe?g|webp)$", re.IGNORECASE)

MediaKind = Literal["frame", "video", "reference"]
"""A submitted file's role, as the prompt text addresses it.

Shared by `SamplingRecord.submitted_files` here and `MediaPayload.kind` in
`providers.base`, so a misspelled kind is a type error where it is built
instead of a silently mislabelled attachment in the reviewer prompt.
"""


@dataclass(frozen=True)
class ClipMedia:
    """Everything discoverable about one submission source."""

    frames: tuple[Path, ...]
    """Frame files, sorted by their numeric index."""
    video: Path | None
    """A muxed video file beside the frames, if one exists."""
    log: Path | None
    """The capture's client log, if one sits beside the frames."""
    source: Path
    """The exact path the caller passed (file or directory)."""


@dataclass(frozen=True)
class SamplingRecord:
    """What the review actually submitted, and what it dropped to get there."""

    frames_available: int
    frames_submitted: int
    sampled: bool
    frame_indices: tuple[int, ...]
    """Position in the clip's own frame order of each submitted frame, in
    submission order. Empty when a video went instead.

    An issue's `at_frame` names one of these positions, so without them the
    index is unresolvable once sampling dropped frames: a consumer would have
    to re-derive the spacing arithmetic to learn which file a critique points
    at, and a re-derivation that rounds differently points at a different
    frame."""
    submitted_files: tuple[tuple[str, MediaKind], ...]
    """(path, kind) for every file sent, in submission order."""
    note: str


def discover(source: Path) -> ClipMedia:
    """Resolve a clip file or directory into its frames, video, and log."""
    if not source.exists():
        raise DeadeyeError(f"no such clip: {source}")
    if source.is_file():
        suffix = source.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return ClipMedia(frames=(source,), video=None, log=None, source=source)
        if suffix in VIDEO_SUFFIXES:
            return ClipMedia(frames=(), video=source, log=None, source=source)
        raise DeadeyeError(
            f"{source} is not a clip this tool reviews: expected a directory of "
            "frames, a muxed video, or an image file"
        )
    if not source.is_dir():
        raise DeadeyeError(f"no such clip: {source}")

    try:
        frames, video, log = _scan_directory(source)
    except OSError as exc:
        # A directory that lists but cannot be read (permissions, I/O fault)
        # is a refusal with the operation named, not an OS traceback.
        raise DeadeyeError(f"cannot read clip directory {source}: {exc}") from exc
    if not frames and video is None:
        raise DeadeyeError(
            f"{source} holds neither frames nor a muxed video; a clip needs at least one"
        )
    return ClipMedia(frames=tuple(frames), video=video, log=log, source=source)


def _scan_directory(directory: Path) -> tuple[list[Path], Path | None, Path | None]:
    """Single-pass directory scan: find frames, muxed video, and log file.

    One readdir serves the whole classification, whatever the file's role: a
    scan per role would walk the directory three times, which matters for a
    clip with many frames. `os.scandir` answers "is this a file?" from the
    directory entry the OS already read, where `Path.is_file()` spends a stat
    syscall per entry. Nothing is sorted until it has to be: numbered frames
    are ordered by index (name breaking a tie, so the order never depends on
    how the filesystem happened to hand the entries over), and the other two
    lists hold the rare video and log, sorted only when they are ambiguous.
    """
    numbered: list[tuple[int, str, Path]] = []
    fallback_images: list[Path] = []
    videos: list[Path] = []
    logs: list[Path] = []

    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file():
                continue
            candidate = Path(entry.path)
            suffix = candidate.suffix.lower()
            if suffix in VIDEO_SUFFIXES:
                videos.append(candidate)
            elif suffix in LOG_SUFFIXES:
                logs.append(candidate)
            elif suffix in IMAGE_SUFFIXES:
                fallback_images.append(candidate)
                # The pattern ends in one of the image suffixes, so a match is
                # impossible for any other entry: a directory that also holds
                # a muxed clip, a client log, and hundreds of unrelated files
                # spent a regex match on each of them for nothing.
                match = _FRAME_RE.match(entry.name)
                if match:
                    numbered.append((int(match.group(1)), entry.name, candidate))

    if numbered:
        numbered.sort(key=lambda item: (item[0], item[1]))
        frames = [path for _, _, path in numbered]
    else:
        frames = sorted(fallback_images, key=lambda path: path.name)
    videos.sort(key=lambda path: path.name)
    logs.sort(key=lambda path: path.name)

    video = _require_single(videos, directory, "muxed video", "review one clip at a time")
    log = _require_single(logs, directory, "log file", "keep the clip self-contained")
    return frames, video, log


def _require_single(matches: list[Path], directory: Path, what: str, remedy: str) -> Path | None:
    """Zero or one match, or a refusal."""
    if len(matches) > 1:
        raise DeadeyeError(
            f"{directory} holds more than one {what} "
            f"({', '.join(p.name for p in matches)}); {remedy}"
        )
    return matches[0] if matches else None


def file_size(path: Path) -> int:
    """A submission file's size, without materializing its contents.

    The one home for the preflight size lookup, so a file that cannot be
    inspected (it vanished between discovery and the budget check, its
    directory is unreadable, the filesystem faulted) is refused with its path
    named instead of escaping as a bare OSError from whichever caller reached
    it first.
    """
    try:
        return path.stat().st_size
    except OSError as exc:
        raise DeadeyeError(f"cannot inspect file {path}: {exc}") from exc


def base64_wire_bytes(size: int) -> int:
    """The size `size` raw media bytes reach the wire as, once base64-encoded.

    Every hosted adapter submits media inline as base64 inside one JSON
    request (3 raw bytes become 4 encoded characters), so a per-request byte
    budget applies to the encoded form. Checking raw file bytes against such
    a budget would wave through a clip the provider then refuses after the
    full upload has already crossed the network.
    """
    return 4 * ((size + 2) // 3)


def sample(
    media: ClipMedia,
    *,
    max_frames: int | None,
    video_capable: bool,
    max_video_bytes: int | None,
    reserved_wire_bytes: int = 0,
) -> SamplingRecord:
    """Pick the media to submit under a provider's limits.

    A provider that can take video gets the muxed file when one exists and is
    under the byte budget; otherwise (or when it cannot take video at all) the
    frame sequence is sampled down to `max_frames` with even spacing, always
    keeping the first and last frame. The record names every file that is
    actually sent and any dropping that happened, so the evidence can say
    exactly what reached the model.

    `reserved_wire_bytes` is the encoded size of media already bound for the
    same request (the intent's reference assets). The budget is a whole-request
    budget, so those bytes are spent before the video decision: a video that
    fits only on its own would be picked here and then push the request over
    the cap, refusing the submission outright where the frame sequence would
    have fit.
    """
    messages: list[str] = []
    if media.video is not None and video_capable:
        size = file_size(media.video)
        # The budget names what the request carries, and the request carries
        # the video base64-encoded: compare the encoded size, never the raw.
        wire = base64_wire_bytes(size)
        figures = f"{wire} as submitted base64"
        if reserved_wire_bytes:
            figures += f" plus {reserved_wire_bytes} for the reference media in the same request"
        if max_video_bytes is not None and wire + reserved_wire_bytes > max_video_bytes:
            if not media.frames:
                # The provider ingests video fine; the file is simply over its
                # byte budget and there is nothing to fall back to. Naming the
                # capability instead would send the operator hunting for a
                # different provider when the clip is what must change.
                raise DeadeyeError(
                    f"{media.video} is {size} bytes ({figures}), "
                    f"over the provider's "
                    f"{max_video_bytes}-byte video budget, and there are no "
                    "frames to sample instead; shorten or recompress the clip"
                )
            messages.append(
                f"muxed video {flat_label_text(media.video.name)} is {size} bytes "
                f"({figures}), over "
                f"the provider's {max_video_bytes}-byte video budget; sampled frames instead"
            )
        else:
            return SamplingRecord(
                frames_available=len(media.frames),
                frames_submitted=0,
                sampled=False,
                frame_indices=(),
                submitted_files=((str(media.video), "video"),),
                note=f"submitted muxed video {flat_label_text(media.video.name)} ({size} bytes)",
            )

    frames = list(media.frames)
    available = len(frames)
    if not frames:
        if media.video is None:
            # Unreachable through discover(), which refuses a clip holding
            # neither frames nor video; kept so a hand-built ClipMedia with
            # neither cannot pass silently.
            raise DeadeyeError(f"{media.source} holds no media to submit")
        raise DeadeyeError(
            f"{media.source} has a video but the provider cannot ingest video and "
            "no frames are available to sample; the provider does not meet this "
            "capability"
        )
    if max_frames is not None and available > max_frames:
        frame_indices = _evenly_spaced_indices(available, max_frames)
        messages.append(
            f"sampled {available} frames down to {max_frames} (even spacing, first and last kept)"
        )
        sampled = True
    else:
        frame_indices = tuple(range(available))
        sampled = False
        messages.append("submitted the full frame sequence")
    selected = [frames[index] for index in frame_indices]
    return SamplingRecord(
        frames_available=available,
        frames_submitted=len(selected),
        sampled=sampled,
        frame_indices=frame_indices,
        submitted_files=tuple((str(path), "frame") for path in selected),
        note="; ".join(messages),
    )


def _evenly_spaced_indices(available: int, count: int) -> tuple[int, ...]:
    """The `count` frame positions to submit out of `available`, evenly spaced.

    Positions, not files: the record carries them so an `at_frame` a model
    names resolves to the frame it saw, and the caller reads the files out of
    the clip's own order.

    Always keeps the first and last. The step (available - 1) / (count - 1) is
    strictly greater than 1 when count < available, so two adjacent rounded
    indices can never collide: submitting fewer frames than the provider's
    limit allows would be a silent loss. Index 0 maps to the first frame and
    count - 1 to the last.
    """
    if count <= 0:
        raise DeadeyeError("provider frame limit must be a positive number of frames")
    if count >= available:
        return tuple(range(available))
    if count == 1:
        return (0,)
    return tuple(sorted(round(i * (available - 1) / (count - 1)) for i in range(count)))


def flat_label_text(value: str) -> str:
    """A filename made safe to interpolate into reviewer-prompt text.

    Filenames are authored-local untrusted text that reaches the model both
    outside the author-statement fence (attachment labels) and inside it (the
    reference listing, the media summary). A name carrying a newline or any
    other control character could forge extra label-shaped lines there; every
    non-printable character becomes a space. Evidence keeps the true path;
    only prompt-facing renderings are flattened.

    `isprintable()` settles the common name on its own: a string with nothing
    to flatten is returned unchanged, so the per-character Python walk runs
    only for the hostile names this exists to catch.
    """
    if value.isprintable():
        return value
    return "".join(char if char.isprintable() else " " for char in value)


def mime_for_suffix(suffix: str) -> str:
    """The MIME name for a submitted file's suffix, or a refusal."""
    suffix = suffix.lower()
    try:
        return MIME_BY_SUFFIX[suffix]
    except KeyError:
        raise DeadeyeError(
            f"no MIME type is known for {suffix!r}; accepted suffixes are "
            + ", ".join(sorted(MIME_BY_SUFFIX))
        ) from None
