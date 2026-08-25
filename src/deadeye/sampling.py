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

from .errors import DeadeyeError

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov")
LOG_SUFFIXES = (".log",)

_FRAME_RE = re.compile(r"^frame-(\d+)\.(?:png|jpe?g|webp)$", re.IGNORECASE)


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
    submitted_files: tuple[tuple[str, str], ...]
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

    frames = _frames_in(source)
    video = _single_video_in(source)
    log = _single_log_in(source)
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


def _single_video_in(directory: Path) -> Path | None:
    videos = [
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in VIDEO_SUFFIXES
    ]
    if len(videos) > 1:
        raise DeadeyeError(
            f"{directory} holds more than one muxed video ({', '.join(p.name for p in videos)}); "
            "review one clip at a time"
        )
    return videos[0] if videos else None


def _single_log_in(directory: Path) -> Path | None:
    logs = [
        candidate
        for candidate in directory.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in LOG_SUFFIXES
    ]
    if len(logs) > 1:
        raise DeadeyeError(
            f"{directory} holds more than one log file ({', '.join(p.name for p in logs)}); "
            "keep the clip self-contained"
        )
    return logs[0] if logs else None


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
            note = (
                f"muxed video {media.video.name} is {size} bytes, over the provider's "
                f"{max_video_bytes}-byte video budget; sampled frames instead"
            )
        else:
            return SamplingRecord(
                frames_available=len(media.frames),
                frames_submitted=0,
                sampled=False,
                submitted_files=((str(media.video), "video"),),
                note=f"submitted muxed video {media.video.name} ({size} bytes)",
            )
    else:
        note = ""

    frames = list(media.frames)
    available = len(frames)
    if not frames:
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
    indices = {round(index * (len(frames) - 1) / (count - 1)) for index in range(count)}
    indices.add(0)
    indices.add(len(frames) - 1)
    return [frames[index] for index in sorted(indices)][:count]


def mime_for_suffix(suffix: str) -> str:
    """The MIME name for a submitted file's suffix, or a refusal."""
    suffix = suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }[suffix]
    if suffix in VIDEO_SUFFIXES:
        return {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
        }[suffix]
    raise DeadeyeError(
        f"no MIME type is known for {suffix!r}; accepted suffixes are "
        + ", ".join(sorted(IMAGE_SUFFIXES + VIDEO_SUFFIXES))
    )
