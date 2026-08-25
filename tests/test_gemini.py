"""Gemini adapter: offline-pinnable surface, opt-in live run.

The HTTP path cannot run without a credential, so it is covered by an
opt-in live test (`DEADEYE_NETWORK_TESTS=gemini` + `GEMINI_API_KEY`), never by
the offline suite. Everything else — limits, MIME mapping, credential
presence, the request body shape the adapter would send — is pinned offline.
"""

from __future__ import annotations

import io
import os

import pytest

from deadeye.errors import DeadeyeError
from deadeye.providers.base import MediaPayload
from deadeye.providers.gemini import (
    GeminiProvider,
)


def test_limits_declare_video_and_frames() -> None:
    limits = GeminiProvider().limits
    assert limits.accepts_video
    assert limits.max_frames is not None and limits.max_frames > 0
    assert ".mp4" in limits.suffixes
    assert ".png" in limits.suffixes


def test_credential_presence_never_contacts_the_provider(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    provider = GeminiProvider()
    assert not provider.is_configured()
    assert "GEMINI_API_KEY" in provider.configuration_hint()
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    assert provider.is_configured()


def test_review_without_credential_refuses_locally(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from deadeye.providers.base import ReviewRequest

    request = ReviewRequest(prompt="p", media=(), model="m", timeout_seconds=1.0)
    with pytest.raises(DeadeyeError, match="no credential"):
        GeminiProvider().review(request)


class _FakeResponse(io.BytesIO):
    """A urlopen stand-in: a context manager carrying one JSON body."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _review_request():
    from deadeye.providers.base import ReviewRequest

    return ReviewRequest(prompt="p", media=(), model="m", timeout_seconds=1.0)


def test_a_connection_fault_mid_response_is_a_refusal_not_a_crash(monkeypatch, http_opener) -> None:
    """A reset or truncated body after the request was billed must surface
    as one DeadeyeError, never as a raw ConnectionResetError traceback."""
    import http.client

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    faults = [
        ConnectionResetError("connection reset by peer"),
        http.client.IncompleteRead(b"partial", 100),
    ]
    for fault in faults:

        def broken_urlopen(request, timeout, _fault=fault):
            raise _fault

        http_opener(broken_urlopen)
        with pytest.raises(DeadeyeError, match="new billable review"):
            GeminiProvider().review(_review_request())


def test_a_refused_review_closes_the_error_body(monkeypatch, http_opener) -> None:
    """The HTTP error body owns the request's socket until it is closed: a
    refused review must release it explicitly, or the long-lived MCP server
    accumulates one dead connection per failure until cyclic GC reclaims the
    exception chain."""
    import urllib.error

    monkeypatch.setenv("GEMINI_API_KEY", "k")

    class _TrackingBody(io.BytesIO):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    body = _TrackingBody(b'{"error": {"message": "quota exhausted"}}')
    error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/test",
        429,
        "Too Many Requests",
        {},
        body,
    )

    def refused_urlopen(request, timeout):
        raise error

    http_opener(refused_urlopen)
    with pytest.raises(DeadeyeError, match="HTTP 429"):
        GeminiProvider().review(_review_request())
    assert body.closed


def test_a_null_content_block_does_not_crash_the_adapter(monkeypatch, http_opener) -> None:
    """Gemini can answer `content: null` under a safety block; the adapter
    reads it as an empty candidate instead of dying on AttributeError."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    envelope = {"candidates": [{"finishReason": "STOP", "content": None}]}
    http_opener(lambda request, timeout: _FakeResponse(json_module.dumps(envelope).encode()))
    response = GeminiProvider().review(_review_request())
    assert response.raw_text == ""


def test_a_non_ascii_model_name_is_percent_encoded_into_the_url(monkeypatch, http_opener) -> None:
    """The model is one URL path segment: a space or non-ASCII character must
    ride as percent-encoded UTF-8, never as raw request-line bytes."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    envelope = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}
    seen: dict = {}

    def capture_urlopen(request, timeout):
        seen["url"] = request.full_url
        return _FakeResponse(json_module.dumps(envelope).encode())

    http_opener(capture_urlopen)
    from deadeye.providers.base import ReviewRequest

    request = ReviewRequest(prompt="p", media=(), model="gemín 2.5 flash", timeout_seconds=1.0)
    response = GeminiProvider().review(request)
    assert response.raw_text == "ok"
    assert seen["url"].endswith("/gem%C3%ADn%202.5%20flash:generateContent")


def _capture_body(monkeypatch, http_opener, envelope: dict) -> dict:
    """POST through a stub opener; return the JSON body the adapter built."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen: dict = {}

    def capture_urlopen(request, timeout):
        seen["body"] = json_module.loads(request.data.decode("utf-8"))
        return _FakeResponse(json_module.dumps(envelope).encode())

    http_opener(capture_urlopen)
    return seen


_ENVELOPE = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}


def test_the_generation_is_capped_against_runaway_output(monkeypatch, http_opener) -> None:
    """No cap means an unbounded billable generation when the model loops;
    the adapter must always send one."""
    from deadeye.providers.gemini import DEFAULT_MAX_OUTPUT_TOKENS

    seen = _capture_body(monkeypatch, http_opener, _ENVELOPE)
    GeminiProvider().review(_review_request())
    generation = seen["body"]["generationConfig"]
    assert generation["maxOutputTokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert generation["response_mime_type"] == "application/json"


def test_max_output_tokens_can_be_overridden_by_config(monkeypatch, http_opener, tmp_path) -> None:
    from deadeye import config

    (tmp_path / "config.local.toml").write_text(
        "[providers.gemini]\nmax_output_tokens = 1024\n", encoding="utf-8"
    )
    monkeypatch.setenv("DEADEYE_CONFIG_DIR", str(tmp_path))
    config.reset()
    try:
        seen = _capture_body(monkeypatch, http_opener, _ENVELOPE)
        GeminiProvider().review(_review_request())
        assert seen["body"]["generationConfig"]["maxOutputTokens"] == 1024
    finally:
        config.reset()


@pytest.mark.skipif(
    os.environ.get("DEADEYE_NETWORK_TESTS") != "gemini" or not os.environ.get("GEMINI_API_KEY"),
    reason="opt-in live run: set DEADEYE_NETWORK_TESTS=gemini and GEMINI_API_KEY",
)
def test_live_gemini_reviews_a_frame_sequence(tmp_path) -> None:
    from deadeye.providers.base import ReviewRequest

    clip = tmp_path / "clip"
    clip.mkdir()
    # A tiny solid-colour PNG, so the live run submits real image bytes.
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

    for index in range(3):
        (clip / f"frame-{index:04d}.png").write_bytes(solid_png((40, 40, 40)))

    provider = GeminiProvider()
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
