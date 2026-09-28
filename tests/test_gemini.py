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


@pytest.mark.parametrize(
    "candidates",
    [{"content": {}}, ["not an object"]],
)
def test_an_invalid_candidate_list_is_a_refusal_not_an_attribute_error(
    monkeypatch, http_opener, candidates
) -> None:
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    http_opener(
        lambda request, timeout: _FakeResponse(
            json_module.dumps({"candidates": candidates}).encode()
        )
    )

    with pytest.raises(DeadeyeError, match=r"invalid candidate|no candidate"):
        GeminiProvider().review(_review_request())


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


def test_a_truncated_generation_is_a_refusal_not_a_half_verdict(monkeypatch, http_opener) -> None:
    """A finishReason the adapter does not recognise (a safety block, a
    recitation stop) means the model never finished the verdict. A truncated
    JSON fragment must be refused, not parsed into a half-scored result that
    would read like real evidence."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    envelope = {
        "candidates": [
            {
                "finishReason": "SAFETY",
                "content": {"parts": [{"text": '{"confidence": 0.9, "issues": ['}]},
            }
        ]
    }
    http_opener(lambda request, timeout: _FakeResponse(json_module.dumps(envelope).encode()))
    with pytest.raises(DeadeyeError, match="ended the response early"):
        GeminiProvider().review(_review_request())


def test_a_complete_generation_reports_usage_and_the_model_it_came_from(
    monkeypatch, http_opener
) -> None:
    """Usage and the model version the provider reports ride into the
    evidence envelope, so a later reader can tell a cheap run from an
    expensive one and a substituted model from the requested one."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    envelope = {
        "modelVersion": "gemini-2.5-flash",
        "usageMetadata": {"totalTokenCount": 91, "promptTokenCount": 40},
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}],
    }
    http_opener(lambda request, timeout: _FakeResponse(json_module.dumps(envelope).encode()))
    response = GeminiProvider().review(_review_request())
    assert response.raw_text == "ok"
    assert response.model_reported == "gemini-2.5-flash"
    assert response.usage == {"totalTokenCount": 91, "promptTokenCount": 40}


def test_a_usage_block_that_is_not_an_object_is_dropped_not_wrapped(
    monkeypatch, http_opener
) -> None:
    """A provider that answers `usageMetadata: "n/a"` must not put a bare
    string into the evidence envelope, where a reader would treat it as a
    number-shaped field. The verdict still stands; only the usage is dropped."""
    import json as json_module

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    envelope = {
        "modelVersion": "gemini-2.5-flash",
        "usageMetadata": "n/a",
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}],
    }
    http_opener(lambda request, timeout: _FakeResponse(json_module.dumps(envelope).encode()))
    response = GeminiProvider().review(_review_request())
    assert response.usage is None
    assert response.raw_text == "ok"


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


def test_max_output_tokens_can_be_overridden_by_config(
    monkeypatch, http_opener, isolated_config
) -> None:
    (isolated_config / "config.local.toml").write_text(
        "[providers.gemini]\nmax_output_tokens = 1024\n", encoding="utf-8"
    )
    from deadeye import config

    config.reset()
    seen = _capture_body(monkeypatch, http_opener, _ENVELOPE)
    GeminiProvider().review(_review_request())
    assert seen["body"]["generationConfig"]["maxOutputTokens"] == 1024


def test_a_non_positive_output_cap_is_refused_before_submission(
    monkeypatch, isolated_config
) -> None:
    """A cap is the only thing between a looping generation and unbounded
    spend, and a provider that reads zero or a negative cap as 'no limit'
    turns a botched key into exactly that. The refusal names the key."""
    from deadeye import config

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for value in ("0", "-1"):
        (isolated_config / "config.local.toml").write_text(
            f"[providers.gemini]\nmax_output_tokens = {value}\n", encoding="utf-8"
        )
        config.reset()
        with pytest.raises(DeadeyeError, match="at least 1"):
            GeminiProvider().review(_review_request())


def test_the_instruction_travels_as_the_system_instruction() -> None:
    # The instruction the model must obey is pipeline-owned; the authored
    # intent is not. `systemInstruction` is Gemini's own role, so intent text
    # cannot occupy the slot the contract and rubric sit in.
    from deadeye.providers.base import ReviewRequest
    from deadeye.providers.gemini import build_body

    body = build_body(
        ReviewRequest(
            prompt="the author's statement, fenced",
            system_prompt="You are reviewing a game-asset candidate on screen.",
            media=(),
            model="m",
            timeout_seconds=1.0,
        )
    )
    system = body["systemInstruction"]["parts"]
    assert [part["text"] for part in system] == [
        "You are reviewing a game-asset candidate on screen."
    ]
    user_text = [part["text"] for part in body["contents"][0]["parts"] if "text" in part]
    assert "the author's statement, fenced" in user_text
    assert not any("game-asset candidate" in text for text in user_text)


def test_a_caller_with_no_system_instruction_sends_none(monkeypatch, http_opener) -> None:
    from deadeye.providers.base import ReviewRequest
    from deadeye.providers.gemini import build_body

    body = build_body(ReviewRequest(prompt="p", media=(), model="m", timeout_seconds=1.0))
    assert "systemInstruction" not in body
    sent = _capture_body(monkeypatch, http_opener, _ENVELOPE)
    GeminiProvider().review(ReviewRequest(prompt="p", media=(), model="m", timeout_seconds=1.0))
    assert "systemInstruction" not in sent["body"]


@pytest.mark.skipif(
    os.environ.get("DEADEYE_NETWORK_TESTS") != "gemini" or not os.environ.get("GEMINI_API_KEY"),
    reason="opt-in live run: set DEADEYE_NETWORK_TESTS=gemini and GEMINI_API_KEY",
)
def test_live_gemini_reviews_a_frame_sequence(tmp_path, solid_png) -> None:
    from deadeye.providers.base import ReviewRequest

    clip = tmp_path / "clip"
    clip.mkdir()
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
