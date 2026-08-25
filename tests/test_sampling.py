"""Clip discovery and frame sampling."""

from __future__ import annotations

import pytest

from deadeye.errors import DeadeyeError
from deadeye.sampling import discover, sample


def test_discover_reads_a_playtest_clip_directory(clip_dir_with_video) -> None:
    media = discover(clip_dir_with_video)
    assert len(media.frames) == 10
    assert media.frames[0].name == "frame-0000.png"
    assert media.frames[-1].name == "frame-0009.png"
    assert media.video is not None and media.video.name == "clip.mp4"
    assert media.log is not None and media.log.name == "client.log"


def test_discover_sorts_frames_by_number_not_name(clip_dir_with_video) -> None:
    # frame-0010.png would sort before frame-0002.png lexically; the numeric
    # sort keeps the sequence honest.
    (clip_dir_with_video / "frame-0010.png").write_bytes(b"x")
    media = discover(clip_dir_with_video)
    assert media.frames[1].name == "frame-0001.png"
    assert media.frames[-1].name == "frame-0010.png"


def test_discover_accepts_a_single_video_or_image_file(tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    assert discover(video).video == video
    image = tmp_path / "frame.png"
    image.write_bytes(b"i")
    assert discover(image).frames == (image,)


def test_discover_refuses_an_empty_directory(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DeadeyeError, match="neither frames nor a muxed video"):
        discover(empty)


def test_an_unreadable_clip_directory_is_a_refusal_not_a_traceback(tmp_path, monkeypatch) -> None:
    """A directory that exists but cannot be listed (permissions, I/O fault)
    refuses with the operation named instead of leaking an OS exception."""
    from deadeye import sampling

    unreadable = tmp_path / "locked"
    unreadable.mkdir()

    def forbidden(directory):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sampling, "_frames_in", forbidden)
    with pytest.raises(DeadeyeError, match="cannot read clip directory"):
        discover(unreadable)


def test_discover_refuses_two_videos_in_one_directory(clip_dir_with_video) -> None:
    (clip_dir_with_video / "second.mp4").write_bytes(b"x")
    with pytest.raises(DeadeyeError, match="more than one muxed video"):
        discover(clip_dir_with_video)


def test_video_is_preferred_when_the_provider_takes_video(clip_dir_with_video) -> None:
    media = discover(clip_dir_with_video)
    record = sample(media, max_frames=8, video_capable=True, max_video_bytes=10_000)
    assert record.submitted_files == ((str(media.video), "video"),)
    assert record.frames_submitted == 0
    assert not record.sampled


def test_video_over_budget_falls_back_to_sampled_frames(clip_dir_with_video) -> None:
    media = discover(clip_dir_with_video)
    record = sample(media, max_frames=4, video_capable=True, max_video_bytes=5)
    assert len(record.submitted_files) == 4
    assert record.sampled
    assert "over the provider's 5-byte video budget" in record.note


def test_frames_are_sampled_evenly_keeping_first_and_last(clip_dir) -> None:
    media = discover(clip_dir)
    record = sample(media, max_frames=4, video_capable=False, max_video_bytes=None)
    names = [path.rsplit("/", 1)[-1] for path, _ in record.submitted_files]
    assert names == ["frame-0000.png", "frame-0003.png", "frame-0006.png", "frame-0009.png"]
    assert record.frames_available == 10
    assert record.frames_submitted == 4
    assert record.sampled
    assert "even spacing, first and last kept" in record.note


def test_frames_under_the_limit_are_untouched(clip_dir) -> None:
    media = discover(clip_dir)
    record = sample(media, max_frames=20, video_capable=False, max_video_bytes=None)
    assert record.frames_submitted == 10
    assert not record.sampled
    assert "full frame sequence" in record.note


def test_video_only_without_frames_refused_when_provider_takes_images_only(
    tmp_path,
) -> None:
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "clip.mp4").write_bytes(b"v")
    with pytest.raises(DeadeyeError, match="cannot ingest video and no frames"):
        sample(discover(clip), max_frames=8, video_capable=False, max_video_bytes=None)
