"""Shared fixtures for the deadeye suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def clip_dir(tmp_path: Path) -> Path:
    """A playtest-shaped clip directory: numbered frames plus a client log.

    No muxed video, so the default fake path exercises frame sampling; the
    video-ingestion tests opt into `clip_dir_with_video`.
    """
    clip = tmp_path / "clip"
    clip.mkdir()
    for index in range(10):
        (clip / f"frame-{index:04d}.png").write_bytes(bytes([index, 0, 0, 0]))
    (clip / "client.log").write_text(
        "2026-08-25 [7dtd-playtest] clip complete demo/thing frames=10\n"
    )
    return clip


@pytest.fixture
def clip_dir_with_video(clip_dir: Path) -> Path:
    """The same clip directory with a muxed video beside the frames."""
    (clip_dir / "clip.mp4").write_bytes(b"fake-mp4-bytes")
    return clip_dir


@pytest.fixture
def intent_path(tmp_path: Path) -> Path:
    path = tmp_path / "thing.review.json"
    path.write_text(
        '{"purpose": "show the garment survives a full turn without clipping", '
        '"subject": "thing (worn garment)", "camera_path": "turntable", '
        '"desired_qualities": "proportions read right from every side", '
        '"avoid": ["clipping", "popping"], "questions": ["does the grip read thin?"], '
        '"suite": "demo", "case": "thing"}'
    )
    return path


@pytest.fixture
def intent_bytes() -> bytes:
    return (
        b'{"purpose": "show the garment survives a full turn without clipping", '
        b'"camera_path": "turntable"}'
    )
