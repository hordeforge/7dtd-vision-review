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
import time
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


def test_a_declared_charset_whose_codec_raises_is_a_fault_not_a_crash(http_opener) -> None:
    """`charset=undefined` resolves to a codec whose decode raises plain
    UnicodeError, which is neither a UnicodeDecodeError nor a LookupError. It
    must take the same fall-back-to-UTF-8 path as a codec that is not known
    at all, not escape as a bare traceback past the fault mapping after a
    billed submission."""

    def answering_open(request, timeout):
        return _CharsetResponse(b'{"modelVersion": "m"}', "application/json; charset=undefined")

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


def test_a_declared_charset_bytes_decode_rejects_is_a_fault_not_a_crash(http_opener) -> None:
    """A Content-Type whose charset parameter is not a usable codec name at
    all must take the same fall-back-to-UTF-8 path as every other unusable
    declaration, not escape as a bare traceback past the fault mapping after a
    billed submission. `bytes.decode` refuses a name carrying an embedded null
    with a plain ValueError before it ever reaches the codec lookup, so it is
    neither a LookupError nor a UnicodeError; both spellings of that name are
    covered here."""

    for charset in ("charset=\0", "charset=\x00bogus"):

        def answering_open(request, timeout, charset=charset):
            return _CharsetResponse(b'{"modelVersion": "m"}', f"application/json; {charset}")

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


def test_an_undecodable_body_with_a_declared_charset_names_both_attempts(http_opener) -> None:
    """When the declared charset cannot be used and the bytes are not UTF-8
    either, the refusal names both attempts: the operator needs to know the
    declared charset did not win, not just that decoding failed."""

    def answering_open(request, timeout):
        return _CharsetResponse(b'{"modelVersion": "caf\xe9"}', "application/json; charset=bogus")

    http_opener(answering_open)
    with pytest.raises(DeadeyeError) as excinfo:
        _post()
    message = str(excinfo.value)
    assert "bogus" in message
    assert "UTF-8" in message


def test_a_trickling_response_body_is_cut_off_at_the_overall_deadline(
    http_opener, monkeypatch
) -> None:
    """`urllib`'s timeout is per socket operation, so a body that keeps
    trickling bytes never trips it. The read loop must still end at the
    submission's own budget: in the long-lived MCP server an unbounded read
    is a stall the caller never hears about, on a request that may already
    have billed server-side."""
    from deadeye.providers import _http

    clock = [0.0]
    monkeypatch.setattr(_http.time, "monotonic", lambda: clock[0])

    class TricklingResponse(io.BytesIO):
        def read1(self, size=-1):  # type: ignore[override]
            clock[0] += 0.4
            return super().read1(size)

    http_opener(lambda request, timeout: TricklingResponse(b"x" * 200_000))
    with pytest.raises(DeadeyeError, match="did not answer within 1s") as excinfo:
        _post()
    # The ambiguous-outcome warning rides the refusal: the submission was
    # sent, so a resubmission is a new billable review, not a retry.
    assert "not a retry of this one" in str(excinfo.value)


def test_a_connection_that_dies_mid_body_is_a_billable_submission_with_no_verdict(
    http_opener,
) -> None:
    """A truncated response is the duplicate-billing case, not a plain fault.

    The request was sent and the provider may finish and bill it while the
    client is left reading a body that stopped. Anything that deduplicates a
    retry has to record that key as spent, which it can only do from the
    exception type: a status the provider refused (a 400, a 429) never
    reached a bill, and this one may have.
    """
    from deadeye.errors import NoVerdictError

    class TruncatedResponse(io.BytesIO):
        def read1(self, size=-1):  # type: ignore[override]
            raise ConnectionResetError("connection reset by peer")

    http_opener(lambda request, timeout: TruncatedResponse(b"{}"))
    with pytest.raises(NoVerdictError, match="not a retry of this one"):
        _post()

    # A provider that refused outright is a different thing: nothing billed,
    # so the key stays free for a corrected retry.
    def refusing_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))

    http_opener(refusing_open)
    with pytest.raises(DeadeyeError) as excinfo:
        _post()
    assert not isinstance(excinfo.value, NoVerdictError)


def test_a_slow_drip_provider_is_refused_at_the_total_budget(http_opener) -> None:
    """The advertised seconds bound the whole call, not one socket read.

    urllib's `timeout=` is a per-operation timeout: a provider answering a few
    bytes every 50ms resets that clock on every read, so the submission runs
    indefinitely and keeps billing while it never finishes. The budget is
    enforced on a monotonic deadline across the reads, so the drip ends as the
    same timeout refusal a stalled provider gets.
    """

    class DrippingResponse(io.BytesIO):
        def read1(self, size=-1):  # type: ignore[override]
            time.sleep(0.05)
            return b"{"

    http_opener(lambda request, timeout: DrippingResponse(b""))
    started = time.monotonic()
    with pytest.raises(DeadeyeError, match=r"did not answer within 0\.3s"):
        post_json(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            body={},
            headers={"x-goog-api-key": "k"},
            timeout_seconds=0.3,
            credential_env="GEMINI_API_KEY",
        )
    # The refusal lands on the budget, not a read-time multiple past it.
    assert time.monotonic() - started < 1.0
