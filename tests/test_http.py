"""The shared HTTP submission boundary (`providers/_http.post_json`).

The adapters' offline tests pin their own fault mapping through the stub
opener; this module pins the properties that must hold against the real
urllib machinery, without any network: redirects are never followed, so the
provider credential cannot ride one to another host or scheme, and
provider-controlled error text cannot forge extra stderr lines.
"""

from __future__ import annotations

import email.message
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


def test_a_deeply_nested_envelope_is_refused_not_crashed(http_opener) -> None:
    """Nesting beyond the interpreter limit is a malformed answer, not a bug
    here: it must be refused like any other bad structure (the treatment
    parse_model_json and the MCP loop give theirs), never escaped as a raw
    RecursionError that would tear through the one-error-line contract."""
    body = ("[" * 20000 + "]" * 20000).encode("utf-8")

    def nested_open(request, timeout):
        return io.BytesIO(body)

    http_opener(nested_open)
    with pytest.raises(DeadeyeError, match="nested too deeply"):
        _post()


def test_an_oversized_success_response_is_refused_with_a_bounded_read(
    http_opener, monkeypatch
) -> None:
    """A provider response is retained for parsing, so a bad endpoint must
    not make that allocation unbounded in the MCP server."""
    from deadeye.providers import _http

    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 16)

    class RecordingResponse(io.BytesIO):
        def read(self, size=-1):
            assert size == 17
            return super().read(size)

    http_opener(lambda request, timeout: RecordingResponse(b"x" * 17))
    with pytest.raises(DeadeyeError, match="more than 16 response bytes"):
        _post()


def test_an_oversized_error_body_is_read_only_up_to_the_fault_cap(http_opener, monkeypatch) -> None:
    """A 4xx/5xx body is sliced into the refusal line, so the read that
    feeds that slice must stop at the character budget. `HTTPError.read()`
    with no size would retain a whole media payload on a misconfigured
    endpoint for the lifetime of the exception chain."""
    from deadeye.providers import _http

    monkeypatch.setattr(_http, "_MAX_FAULT_BODY_CHARS", 8)

    class RecordingBody(io.BytesIO):
        def read(self, size=-1):  # type: ignore[override]
            assert size != -1
            assert 0 < size <= 8
            return super().read(size)

    body = RecordingBody(b"x" * 10_000)

    def refusing_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, body)

    http_opener(refusing_open)
    with pytest.raises(DeadeyeError, match="HTTP 500") as excinfo:
        _post()
    assert "xxxxxxxx" in str(excinfo.value)
    assert body.closed


class _CharsetResponse(io.BytesIO):
    """A BytesIO carrying a Content-Type header, like a real HTTPResponse."""

    def __init__(self, data: bytes, content_type: str) -> None:
        super().__init__(data)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type


def test_a_non_utf8_success_body_is_refused_not_crashed(http_opener) -> None:
    """An invalid byte in a 200 body is an undecodable envelope. It must end
    as one refusal naming the provider (the fault family every other malformed
    answer maps to), not escape as a bare UnicodeDecodeError past this
    module's mapping after the submission was already billed."""
    body = b'{"choices": [{"\xff": 1}]}'

    def answering_open(request, timeout):
        return io.BytesIO(body)

    http_opener(answering_open)
    with pytest.raises(DeadeyeError, match="not valid UTF-8"):
        _post()


def test_the_declared_charset_decodes_the_body(http_opener) -> None:
    """A charset the provider declares in Content-Type wins over the UTF-8
    default; latin-1 text decodes into the real characters instead of being
    refused or silently replaced."""
    body = '{"modelVersion": "caf\xe9-model"}'.encode("iso-8859-1")

    def answering_open(request, timeout):
        return _CharsetResponse(body, "application/json; charset=latin-1")

    http_opener(answering_open)
    envelope = post_json(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        body={},
        headers={"x-goog-api-key": "k"},
        timeout_seconds=1.0,
        credential_env="GEMINI_API_KEY",
    )
    assert envelope["modelVersion"] == "café-model"


def test_an_unknown_declared_charset_falls_back_to_utf8(http_opener) -> None:
    """A Content-Type naming a codec this interpreter does not know falls back
    to JSON's default encoding instead of crashing on the lookup."""

    def answering_open(request, timeout):
        return _CharsetResponse(b'{"modelVersion": "m"}', "application/json; charset=bogus")

    http_opener(answering_open)
    envelope = post_json(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
        body={},
        headers={"x-goog-api-key": "k"},
        timeout_seconds=1.0,
        credential_env="GEMINI_API_KEY",
    )
    assert envelope["modelVersion"] == "m"
