"""NVIDIA NIM adapter: offline-pinnable surface, opt-in live run.

The HTTP path cannot run without a credential, so it is covered by an
opt-in live test (`DEADEYE_NETWORK_TESTS=nvidia` + `NVIDIA_API_KEY`), never
by the offline suite. Everything else — limits, MIME mapping, credential
presence, and the exact request body the adapter would send (including that
frames travel as base64 data URLs, never paths) — is pinned offline.
"""

from __future__ import annotations

import base64
import os

import pytest

from deadeye.errors import DeadeyeError
from deadeye.providers.base import MediaPayload, ProviderLimits, ReviewRequest
from deadeye.providers.nvidia import (
    DEFAULT_MODEL,
    MIME_BY_SUFFIX,
    NvidiaProvider,
    build_body,
    _label_for,
)


def test_limits_declare_images_only() -> None:
    limits = NvidiaProvider().limits
    assert not limits.accepts_video
    assert limits.max_frames is not None and limits.max_frames > 0
    assert ".png" in limits.suffixes
    assert ".mp4" not in limits.suffixes


def test_mime_mapping_covers_the_image_suffixes() -> None:
    assert MIME_BY_SUFFIX[".png"] == "image/png"
    assert MIME_BY_SUFFIX[".webp"] == "image/webp"


def test_credential_presence_never_contacts_the_provider(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    provider = NvidiaProvider()
    assert not provider.is_configured()
    assert "NVIDIA_API_KEY" in provider.configuration_hint()
    monkeypatch.setenv("NVIDIA_API_KEY", "x")
    assert provider.is_configured()


def test_review_without_credential_refuses_locally(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    request = ReviewRequest(prompt="p", media=(), model="m", timeout_seconds=1.0)
    with pytest.raises(DeadeyeError, match="no credential"):
        NvidiaProvider().review(request)


def test_the_request_body_carries_frames_as_data_urls_never_paths() -> None:
    frame = MediaPayload(
        name="frame-0000.png",
        mime_type="image/png",
        kind="frame",
        data=b"\x89PNG-bytes",
    )
    reference = MediaPayload(
        name="good.png",
        mime_type="image/png",
        kind="reference",
        data=b"known-good",
    )
    body = build_body(
        ReviewRequest(
            prompt="review this", media=(frame, reference), model="m", timeout_seconds=1.0
        )
    )
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "review this"}
    labels = [part for part in content if part.get("type") == "text"][1:]
    assert labels == [
        {"type": "text", "text": "frame attachment: frame-0000.png"},
        {"type": "text", "text": "reference image: good.png"},
    ]
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert len(image_parts) == 2
    first = image_parts[0]["image_url"]["url"]
    assert isinstance(first, str)
    assert first.startswith("data:image/png;base64,")
    expected = "data:image/png;base64," + base64.b64encode(frame.data).decode("ascii")
    assert first == expected
    # The frames are the bytes, not the filesystem paths they came from.
    assert "frame-0000.png" not in first
    assert body["model"] == "m"
    assert body["stream"] is False
    assert body["temperature"] == 0.6


def test_a_non_image_payload_is_refused_at_body_build_time() -> None:
    video = MediaPayload(name="clip.mp4", mime_type="video/mp4", kind="video", data=b"v")
    with pytest.raises(DeadeyeError, match="images only"):
        build_body(ReviewRequest(prompt="p", media=(video,), model="m", timeout_seconds=1.0))


def test_the_default_model_is_the_verified_omni_model() -> None:
    assert DEFAULT_MODEL == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def test_attachment_labels_address_the_prompt_order() -> None:
    frame = MediaPayload(name="f.png", mime_type="image/png", kind="frame", data=b"")
    reference = MediaPayload(name="r.png", mime_type="image/png", kind="reference", data=b"")
    assert _label_for(frame) == "frame attachment: f.png"
    assert _label_for(reference) == "reference image: r.png"


@pytest.mark.skipif(
    os.environ.get("DEADEYE_NETWORK_TESTS") != "nvidia"
    or not NvidiaProvider().is_configured(),
    reason="opt-in live run: set DEADEYE_NETWORK_TESTS=nvidia and configure an "
    "NVIDIA key (env or config.local.toml)",
)
def test_live_nvidia_reviews_a_frame_sequence(tmp_path) -> None:  # noqa: ANN001
    import struct
    import zlib

    def solid_png(colour: tuple[int, int, int]) -> bytes:
        width = height = 16
        raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        idat = chunk(b"IDAT", zlib.compress(raw))
        iend = chunk(b"IEND", b"")
        return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend

    clip = tmp_path / "clip"
    clip.mkdir()
    for index in range(3):
        (clip / f"frame-{index:04d}.png").write_bytes(solid_png((40, 40, 40)))

    provider = NvidiaProvider()
    request = ReviewRequest(
        prompt="describe what you see in one sentence",
        media=tuple(
            MediaPayload(
                name=path.name, mime_type="image/png", kind="frame", data=path.read_bytes()
            )
            for path in sorted(clip.iterdir())
        ),
        model=provider.default_model,
        timeout_seconds=120.0,
    )
    response = provider.review(request)
    assert response.raw_text.strip()
    assert response.model_reported
