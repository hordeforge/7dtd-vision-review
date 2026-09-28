"""Shared fixtures for the deadeye suite."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private, empty config directory and a clean process-wide cache.

    The checkout's own `config.toml` / `config.local.toml` must never leak
    into an assertion, so the environment points at a directory the test owns
    and the cache is dropped on both sides of the test.
    """
    from deadeye import config

    config.reset()
    directory = tmp_path / "cfg"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv("DEADEYE_CONFIG_DIR", str(directory))
    yield directory
    config.reset()


@pytest.fixture
def minimal_intent(tmp_path: Path) -> Path:
    """An intent file carrying only the one required field."""
    path = tmp_path / "i.json"
    path.write_text('{"purpose": "show the asset in motion"}', encoding="utf-8")
    return path


@pytest.fixture
def solid_png() -> Callable[[tuple[int, int, int]], bytes]:
    """A solid-colour PNG, so an opt-in live run submits real image bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    def encode(colour: tuple[int, int, int]) -> bytes:
        width = height = 16
        raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        idat = chunk(b"IDAT", zlib.compress(raw))
        iend = chunk(b"IEND", b"")
        return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend

    return encode


@pytest.fixture
def http_opener(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[..., Any]], None]:
    """Route `post_json`'s HTTP calls through a stub opener, offline.

    The stand-in is any callable with the `_OPENER.open` signature,
    `(request, timeout)`, so each test keeps its exact behavior.
    """
    from deadeye.providers import _http

    def install(behavior: Callable[..., Any]) -> None:
        monkeypatch.setattr(_http, "_OPENER", SimpleNamespace(open=behavior))

    return install


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


@pytest.fixture(autouse=True)
def empty_idempotency_ledger() -> Iterator[None]:
    """Start every test with an empty MCP idempotency ledger.

    The ledger is process-wide by design (one long-lived server), so a test
    that names an idempotency key would otherwise leave an entry behind for
    whichever test runs next.
    """
    from deadeye import mcp

    mcp._COMPLETED.clear()
    yield
    mcp._COMPLETED.clear()
