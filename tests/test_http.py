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
from types import SimpleNamespace

import pytest

from deadeye.errors import DeadeyeError, NoVerdictError
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
    RecursionError that would tear through the one-error-line contract.

    The provider did answer, so it is a spent submission: the type is what a
    deduplicating caller reads to know the key may not be retried."""
    body = ("[" * 20000 + "]" * 20000).encode("utf-8")

    def nested_open(request, timeout):
        return io.BytesIO(body)

    http_opener(nested_open)
    with pytest.raises(NoVerdictError, match="nested too deeply"):
        _post()


def test_an_oversized_success_response_is_refused_with_a_bounded_read(
    http_opener, monkeypatch
) -> None:
    """A provider response is retained for parsing, so a bad endpoint must
    not make that allocation unbounded in the MCP server."""
    from deadeye.providers import _http

    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 16)

    sizes: list[int] = []

    class RecordingResponse(io.BytesIO):
        # `read1`, not `read`: `_read_chunk` prefers it when the response
        # offers it, and `io.BytesIO` always does. Overriding `read` here
        # would leave the assertion below never reached.
        def read1(self, size=-1):  # type: ignore[override]
            sizes.append(size)
            return super().read1(size)

    http_opener(lambda request, timeout: RecordingResponse(b"x" * 17))
    with pytest.raises(DeadeyeError, match="more than 16 response bytes"):
        _post()
    # One byte past the cap, so the loop can tell "at the cap" from "over it".
    assert sizes == [17]


def test_an_oversized_error_body_is_read_only_up_to_the_fault_cap(http_opener, monkeypatch) -> None:
    """A 4xx/5xx body is sliced into the refusal line, so the read that
    feeds that slice must stop at the byte budget that covers the character
    budget. `HTTPError.read()` with no size would retain a whole media payload
    on a misconfigured endpoint for the lifetime of the exception chain."""
    from deadeye.providers import _http

    monkeypatch.setattr(_http, "_MAX_FAULT_BODY_CHARS", 8)
    read_cap = 8 * _http._MAX_BYTES_PER_CHAR

    class RecordingBody(io.BytesIO):
        def read(self, size=-1):  # type: ignore[override]
            assert size != -1
            assert 0 < size <= read_cap
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


def test_the_declared_charset_also_decodes_a_fault_body(http_opener) -> None:
    """A refusal line is the only account of a failed submission, so the error
    path decodes on the same rule as the success path. Reading a
    `charset=latin-1` error body as UTF-8 with `errors="replace"` turned every
    non-ASCII character of the provider's own explanation into U+FFFD: the one
    line naming the fault arrived mangled, and two paths answering the same
    provider with two different decoders is how that stops reading as a fault."""
    body = "quota exhausted for caf\xe9".encode("iso-8859-1")
    headers = email.message.Message()
    headers["Content-Type"] = "text/plain; charset=latin-1"

    def refusing_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", headers, io.BytesIO(body)
        )

    http_opener(refusing_open)
    with pytest.raises(DeadeyeError, match="rate-limited") as excinfo:
        _post()
    assert "café" in str(excinfo.value)
    assert "�" not in str(excinfo.value)


def test_a_multibyte_fault_body_is_not_cut_by_the_byte_budget(http_opener, monkeypatch) -> None:
    """`_MAX_FAULT_BODY_CHARS` is a character budget, and the read that feeds
    it was the same number of bytes. A body of three-byte CJK therefore lost
    two thirds of the text the operator's line is supposed to carry, and lost
    its last character to a U+FFFD standing in for the sequence the read
    severed mid-way."""
    from deadeye.providers import _http

    monkeypatch.setattr(_http, "_MAX_FAULT_BODY_CHARS", 8)
    body = "超過".encode() * 8

    def refusing_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, io.BytesIO(body)
        )

    http_opener(refusing_open)
    with pytest.raises(DeadeyeError, match="HTTP 503") as excinfo:
        _post()
    assert "超過" in str(excinfo.value)
    assert "�" not in str(excinfo.value)


def test_an_undecodable_fault_body_still_yields_a_refusal(http_opener) -> None:
    """The fault path replaces an unusable byte where the success path refuses
    it: there is no second submission to protect, and a mangled line beats an
    exception raised while describing one. A body that decodes as neither the
    declared charset nor UTF-8 still names the status, and a header mapping
    that is not an `email.message.Message` must not raise looking one up."""
    body = b"quota exhausted for caf\xe9"

    def refusing_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, io.BytesIO(body)
        )

    http_opener(refusing_open)
    with pytest.raises(DeadeyeError, match="rate-limited") as excinfo:
        _post()
    assert "quota exhausted" in str(excinfo.value)


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


def test_a_slow_drip_provider_is_refused_at_the_total_budget(http_opener, monkeypatch) -> None:
    """The advertised seconds bound the whole call, not one socket read.

    urllib's `timeout=` is a per-operation timeout: a provider answering a few
    bytes every 50ms resets that clock on every read, so the submission runs
    indefinitely and keeps billing while it never finishes. The budget is
    enforced on a monotonic deadline across the reads, so the drip ends as the
    same timeout refusal a stalled provider gets.

    The clock is a counter rather than the wall clock: each read advances it
    past the budget in one step, so the bound is asserted without the test
    waiting for it and without a loaded machine deciding the outcome.
    """
    from deadeye.providers import _http

    clock = [0.0]
    monkeypatch.setattr(_http.time, "monotonic", lambda: clock[0])

    class DrippingResponse(io.BytesIO):
        def read1(self, size=-1):  # type: ignore[override]
            clock[0] += 0.1
            return b"{"

    http_opener(lambda request, timeout: DrippingResponse(b""))
    with pytest.raises(DeadeyeError, match=r"did not answer within 0\.3s"):
        post_json(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
            body={},
            headers={"x-goog-api-key": "k"},
            timeout_seconds=0.3,
            credential_env="GEMINI_API_KEY",
        )
    # The refusal lands on the budget, not several drip intervals past it.
    assert clock[0] <= 0.5


def test_a_transport_fault_after_the_socket_is_up_spends_the_submission(
    http_opener, monkeypatch
) -> None:
    """A `URLError` is two opposite billing outcomes, and only the type is not told apart.

    urllib wraps a host that was never reached and a connection that dropped
    after the request was on the socket in the same exception. Nothing billed
    in the first case; the provider may finish and bill the second. The
    distinction decides whether a deduplicating caller may retry for free, so
    `post_json` reads it off the connection rather than the exception, and the
    spent case has to come out as `NoVerdictError`.
    """
    from deadeye.providers import _http

    def unreachable(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    # Never connected: a plain fault, and the key stays free for a corrected
    # retry.
    http_opener(unreachable)
    with pytest.raises(DeadeyeError, match="could not be reached") as free:
        _post()
    assert not isinstance(free.value, NoVerdictError)

    # Connected, then lost: the same exception type, the spent answer.
    def sent_then_lost(request, timeout):
        _http._SUBMITTED.set(True)
        raise urllib.error.URLError(TimeoutError("timed out writing the request body"))

    http_opener(sent_then_lost)
    with pytest.raises(NoVerdictError, match="not a retry of this one"):
        _post()


def test_the_submitted_marker_follows_the_socket_not_the_exception(monkeypatch) -> None:
    """The marker is set by a completed handshake and by nothing else.

    `HTTPConnection.sock` is assigned only once the TCP handshake returns, so
    a connect that never completes leaves it None and a submission that never
    left stays retryable, while any send on a live socket marks the media as
    gone whether it completed or was cut off partway.
    """
    from deadeye.providers import _http

    connection = _http._TrackedConnection("provider.invalid", timeout=1.0)
    sent: list[bytes] = []
    monkeypatch.setattr(
        type(connection),
        "connect",
        lambda self: setattr(self, "sock", SimpleNamespace(sendall=sent.append)),
    )

    connection.send(b"Content-Length: 2\r\n\r\n")
    assert _http._SUBMITTED.get() is True
    assert sent == [b"Content-Length: 2\r\n\r\n"], "the request bytes still reach the socket"

    # A send that raises after the handshake keeps the marker: a body cut off
    # halfway is exactly the case that may have been delivered in full. The
    # socket is dropped so `send` connects again and takes the failing one.
    _http._SUBMITTED.set(False)
    connection.sock = None
    monkeypatch.setattr(
        type(connection),
        "connect",
        lambda self: setattr(self, "sock", SimpleNamespace(sendall=_refuse)),
    )
    with pytest.raises(ConnectionResetError):
        connection.send(b"{}")
    assert _http._SUBMITTED.get() is True


def _refuse(_data: bytes) -> None:
    raise ConnectionResetError("reset by peer")


def test_the_tracking_handlers_are_the_ones_that_open_connections() -> None:
    """The real opener must route through the tracking handlers, not beside them.

    `build_opener` keeps a default handler whose class a passed handler only
    subclasses, and both sit in the same request chain, so a tracking handler
    added alongside the defaults would sit later in the chain and never be
    reached: every submission would read as never sent, and every spent
    submission would look free again. The chains are asserted here rather than
    assumed, because nothing else in the offline suite can tell.
    """
    from deadeye.providers import _http

    opener = _http._OPENER
    for protocol, expected in (
        ("http", _http._TrackingHTTPHandler),
        ("https", _http._TrackingHTTPSHandler),
    ):
        assert [type(handler) for handler in opener.handle_open[protocol]] == [expected]


def test_the_real_opener_marks_a_submission_it_put_on_a_socket() -> None:
    """The marker is set by the machinery `post_json` really runs.

    The other tests drive a stubbed opener, which cannot catch a tracking
    handler that never reached the request chain. A loopback listener closing
    the connection before it reads anything answers is a submission whose
    bytes went out and whose failure came back as a `URLError`, so it has to
    be classified as spent rather than free.
    """
    import socket
    import threading

    from deadeye.providers import _http

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_drop() -> None:
        connection, _ = listener.accept()
        connection.close()

    server = threading.Thread(target=accept_and_drop, daemon=True)
    server.start()
    try:
        with pytest.raises(NoVerdictError, match="not a retry of this one"):
            post_json(
                "gemini",
                f"http://127.0.0.1:{port}/v1beta/models/m:generateContent",
                body={},
                headers={"x-goog-api-key": "k"},
                timeout_seconds=2.0,
                credential_env="GEMINI_API_KEY",
            )
    finally:
        server.join(timeout=5)
        listener.close()
    assert _http._SUBMITTED.get() is True


def test_a_submission_that_never_opened_a_socket_is_not_marked_sent(http_opener) -> None:
    """The marker is per submission, so a previous review cannot leak into the next.

    The MCP server runs many reviews in one context, and a leftover True would
    make the next unreachable provider read as a spent submission and refuse a
    retry that was in fact free.
    """
    from deadeye.providers import _http

    def unreachable(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    http_opener(unreachable)
    _http._SUBMITTED.set(True)
    with pytest.raises(DeadeyeError, match="could not be reached"):
        _post()
    assert _http._SUBMITTED.get() is False
