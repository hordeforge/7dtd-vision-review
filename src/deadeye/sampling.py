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
        frames = _frames_in(source)
        video = _single_match(source, VIDEO_SUFFIXES, "muxed video", "review one clip at a time")
        log = _single_match(source, LOG_SUFFIXES, "log file", "keep the clip self-contained")
    except OSError as exc:
        # A directory that lists but cannot be read (permissions, I/O fault)
        # is a refusal with the operation named, not an OS traceback.
        raise DeadeyeError(f"cannot read clip directory {source}: {exc}") from exc
    if not frames and video is None:
        raise DeadeyeError(
            f"{source} holds neither frames nor a muxed video; a clip needs at least one"
        )
    return ClipMedia(frames=tuple(frames), video=video, log=log, source=source)


def _frames_in(directory: Path) -> list[Path]:
    numbered: list[tuple[int, Path]] = []
    for candidate in sorted(directory.iterdir()):
        match = _FRAME_RE.match(candidate.name)
        if match:
            numbered.append((int(match.group(1)), candidate))
    if numbered:
        return [path for _, path in sorted(numbered)]
    # A frame directory that does not use the playtest naming is still a frame
    # directory; sort whatever image files exist by name.
    return sorted(
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES
    )


def _single_match(
    directory: Path, suffixes: tuple[str, ...], what: str, remedy: str
) -> Path | None:
    """The one file in `directory` matching `suffixes`, or None; two is a refusal."""
    matches = [
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in suffixes
    ]
    if len(matches) > 1:
        raise DeadeyeError(
            f"{directory} holds more than one {what} "
            f"({', '.join(p.name for p in matches)}); {remedy}"
        )
    return matches[0] if matches else None


def sample(
    media: ClipMedia,
    *,
    max_frames: int | None,
    video_capable: bool,
    max_video_bytes: int | None,
) -> SamplingRecord:
    """Pick the media to submit under a provider's limits.

    A provider that can take video gets the muxed file when one exists and is
    under the byte budget; otherwise (or when it cannot take video at all) the
    frame sequence is sampled down to `max_frames` with even spacing, always
    keeping the first and last frame. The record names every file that is
    actually sent and any dropping that happened, so the evidence can say
    exactly what reached the model.
    """
    if media.video is not None and video_capable:
        size = media.video.stat().st_size
        if max_video_bytes is not None and size > max_video_bytes:
            if not media.frames:
                # The provider ingests video fine; the file is simply over its
                # byte budget and there is nothing to fall back to. Naming the
                # capability instead would send the operator hunting for a
                # different provider when the clip is what must change.
                raise DeadeyeError(
                    f"{media.video} is {size} bytes, over the provider's "
                    f"{max_video_bytes}-byte video budget, and there are no "
                    "frames to sample instead; shorten or recompress the clip"
                )
            note = (
                f"muxed video {flat_label_text(media.video.name)} is {size} bytes, over "
                f"the provider's {max_video_bytes}-byte video budget; sampled frames instead"
            )
        else:
            return SamplingRecord(
                frames_available=len(media.frames),
                frames_submitted=0,
                sampled=False,
                submitted_files=((str(media.video), "video"),),
                note=f"submitted muxed video {flat_label_text(media.video.name)} ({size} bytes)",
            )
    else:
        note = ""

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
    messages = [note] if note else []
    if max_frames is not None and available > max_frames:
        selected = _evenly_spaced(frames, max_frames)
        messages.append(
            f"sampled {available} frames down to {max_frames} (even spacing, first and last kept)"
        )
        sampled = True
    else:
        selected = frames
        sampled = False
        messages.append("submitted the full frame sequence")
    return SamplingRecord(
        frames_available=available,
        frames_submitted=len(selected),
        sampled=sampled,
        submitted_files=tuple((str(path), "frame") for path in selected),
        note="; ".join(messages),
    )


def _evenly_spaced(frames: list[Path], count: int) -> list[Path]:
    """Pick `count` frames with even spacing, always keeping first and last."""
    if count <= 0:
        raise DeadeyeError("provider frame limit must be a positive number of frames")
    if count >= len(frames):
        return frames
    if count == 1:
        return [frames[0]]
    # The step (len - 1) / (count - 1) is strictly greater than 1 here, so the
    # rounded indices are distinct; index 0 maps to the first frame and
    # count - 1 to the last.
    return [
        frames[index]
        for index in sorted(round(i * (len(frames) - 1) / (count - 1)) for i in range(count))
    ]


def flat_label_text(value: str) -> str:
    """A filename made safe to interpolate into reviewer-prompt text.

    Filenames are authored-local untrusted text that reaches the model both
    outside the author-statement fence (attachment labels) and inside it (the
    reference listing, the media summary). A name carrying a newline or any
    other control character could forge extra label-shaped lines there; every
    non-printable character becomes a space. Evidence keeps the true path;
    only prompt-facing renderings are flattened.
    """
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
