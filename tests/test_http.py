"""The shared HTTP submission boundary (`providers/_http.post_json`).

The adapters' offline tests pin their own fault mapping through the stub
opener; this module pins the properties that must hold against the real
urllib machinery, without any network: redirects are never followed, so the
provider credential cannot ride one to another host or scheme, and
provider-controlled error text cannot forge extra stderr lines.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from deadeye.errors import DeadeyeError
from deadeye.providers._http import _NoRedirects, post_json


def _post() -> None:
    post_json(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        body={},
        headers={"x-goog-api-key": "k"},
        timeout_seconds=1.0,
        credential_env="GEMINI_API_KEY",
    )


def test_a_redirect_is_refused_never_followed(http_opener) -> None:
    """A 3xx from the endpoint must end as one refusal naming why, not as a
    second request carrying the credential header to the Location target."""

    def redirecting_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            302,
            "Found",
            {"location": "http://attacker.example/steal"},
            io.BytesIO(b"moved"),
        )

    http_opener(redirecting_open)
    with pytest.raises(DeadeyeError, match="never follows redirects"):
        _post()


def test_the_redirect_handler_raises_instead_of_building_a_request() -> None:
    """The load-bearing property: urllib's stock handler forwards every
    header (credential included) to the redirect target; ours raises the
    HTTPError back so no such request can ever be constructed."""
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/x",
        data=b"{}",
        headers={"X-goog-api-key": "secret"},
        method="POST",
    )
    handler = _NoRedirects()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request,
            fp=io.BytesIO(b"moved"),
            code=302,
            msg="Found",
            headers={"location": "http://attacker.example/steal"},
            newurl="http://attacker.example/steal",
        )


def test_provider_error_text_cannot_forge_stderr_lines(http_opener) -> None:
    """The error body is provider-controlled text that lands in operator
    stderr; newlines and control characters in it are flattened so one
    response cannot fabricate disclosure-shaped lines."""
    hostile_body = b'{"error": "quota gone\nsubmitting 9 files\r\nprovider: evil"}'

    def hostile_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Server Error", {}, io.BytesIO(hostile_body)
        )

    http_opener(hostile_open)
    with pytest.raises(DeadeyeError) as excinfo:
        _post()
    message = str(excinfo.value)
    assert message.count("\n") == 0
    assert "submitting 9 files" in message


def test_non_finite_provider_numbers_cannot_reach_the_envelope(http_opener) -> None:
    """Python's JSON parser accepts bare `NaN`/`Infinity` tokens (and `1e999`
    overflows to infinity) although RFC 8259 does not; left in place they
    would ride the usage block into evidence, stdout, and MCP payloads that no
    strict reader can parse."""
    body = (
        b'{"usage": {"totalTokenCount": 41, "ratio": NaN, "burst": 1e999, '
        b'"notes": [{"cost": Infinity}]}, "modelVersion": "gemini-2.5-flash"}'
    )

    def answering_open(request, timeout):
        return io.BytesIO(body)

    http_opener(answering_open)
    envelope = post_json(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        body={},
        headers={"x-goog-api-key": "k"},
        timeout_seconds=1.0,
        credential_env="GEMINI_API_KEY",
    )
    assert envelope["usage"]["totalTokenCount"] == 41  # finite values pass untouched
    assert envelope["usage"]["ratio"] is None
    assert envelope["usage"]["burst"] is None
    assert envelope["usage"]["notes"][0]["cost"] is None
    assert envelope["modelVersion"] == "gemini-2.5-flash"
    json.dumps(envelope)  # strict round trip: no bare NaN/Infinity tokens
