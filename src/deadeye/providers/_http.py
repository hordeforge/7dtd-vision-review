"""Shared stdlib HTTP submission for the hosted adapters.

Both adapters POST one JSON document and read one JSON envelope back, and
every fault maps to one DeadeyeError naming the provider. A timeout, a
mid-body connection failure, or a request that died on a socket that had
already connected may still have completed and billed server-side, so those
refusals say so explicitly and raise `NoVerdictError`: submitting again is a
new billable review, never a retry. A connection that never came up is the one
transport fault with nothing spent behind it, and it stays a plain
`DeadeyeError` so a deduplicating caller can still retry it for free.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from typing import Any

from ..errors import DeadeyeError, NoVerdictError, did_not_answer, no_verdict
from ..json_safe import loads, strict_json_numbers
from ..prompt_text import flat_label_text

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# How much of a provider's error body may ride in a refusal line: enough to
# name the fault (quota, malformed key) and never a whole payload. Counted in
# characters, which is what the line the operator reads is made of.
_MAX_FAULT_BODY_CHARS = 300
# The most bytes one character occupies in any encoding this module decodes:
# four, for a UTF-8 astral code point. A read budget is in bytes and the
# character budget is not, so the read covers this multiple of it and the
# slice below, on the decoded text, is the one that enforces the limit.
_MAX_BYTES_PER_CHAR = 4
# A successful model response is a compact JSON verdict, not a media stream.
# Bound it so a malformed endpoint or proxy cannot make the long-lived MCP
# server retain an unbounded response body. Eight MiB leaves ample room for a
# 65k-token JSON verdict plus provider metadata.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# What a scrubbed credential reads as in a refusal line.
_REDACTED_CREDENTIAL = "[redacted]"

# A byte-order mark at the head of a body, decoded: the one character a JSON
# parser cannot skip and a refusal line has no business carrying. RFC 8259
# forbids it, but a proxy in front of a provider still emits one, and the
# project's own intent loader already treats a leading BOM as an editor's
# habit rather than an error (`intent._decode_json`). Reading the encoding in
# the data instead of assuming it is the same rule: the BOM names itself, so it
# is honored where it appears and nowhere else.
_BOM = "\ufeff"


def _without_bom(text: str) -> str:
    """`text` with a leading byte-order mark removed, and only a leading one."""
    return text[1:] if text.startswith(_BOM) else text


# Below this length a credential is too short to scrub for: a replacement sweep
# would corrupt ordinary words in the fault text as often as it removed a
# secret, and every provider key this gateway sends is far longer.
_MIN_SCRUBBABLE_CREDENTIAL_CHARS = 8


def scrub_credential(text: str, credential: str | None) -> str:
    """`text` with every occurrence of `credential` removed.

    A fault body is text the endpoint chose, and it reaches a refusal line on
    stderr, in logs, and in whatever reads the CLI's error channel. The
    `endpoint` override exists for a self-hosted proxy, and a proxy that
    answers a refused request by echoing the request it refused hands the
    provider credential straight to every one of those readers. The key-based
    backstop cannot catch that: the body is provider prose, frequently
    truncated, and not always JSON for `redact_json_text` to parse.

    Matching the value the tool already holds needs no parsing at all, so it
    works on a body cut off at the fault cap, in any declared charset, and
    whether or not the credential appears under a key.
    """
    if not credential or len(credential) < _MIN_SCRUBBABLE_CREDENTIAL_CHARS:
        return text
    return text.replace(credential, _REDACTED_CREDENTIAL)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    urllib forwards every request header to the redirect target (only
    content-length/content-type are dropped), so following a 3xx would send
    the provider credential to whatever host and scheme the Location header
    names, including a silent https-to-http downgrade. These JSON API roots
    have no legitimate need to redirect; refusing loudly keeps the credential
    on exactly the host the endpoint override validated.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,  # noqa: ARG002 - the stdlib names it; the raise ignores it
    ) -> urllib.request.Request:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


# Whether the current submission ever put a byte on a socket. `urllib`
# reports a connection that never came up and a connection that dropped while
# the request body was still going out as the same `URLError`, and the two are
# opposite answers to "may this submission already be billed": a refused
# connection reached nothing, while one that died after the handshake sent
# media the provider may finish and bill while the client never sees an answer.
# The exception type cannot separate them, so the connection itself records
# it: `HTTPConnection.sock` is set only once the TCP handshake completes, which
# is exactly the boundary between the two cases. A context variable rather
# than a module global because the marker belongs to one in-flight submission,
# and the MCP server may run reviews concurrently.
_SUBMITTED: ContextVar[bool] = ContextVar("deadeye_submitted", default=False)


class _MarksSubmission(http.client.HTTPConnection):
    """An `HTTPConnection` that records whether a request ever reached a socket.

    Subclassed, never instantiated: the tracking is a mixin over the two
    connection classes the standard handlers open, plain and TLS.
    """

    def send(self, data: Any) -> None:
        try:
            super().send(data)
        finally:
            # In the `finally` because a half-written body is the case that
            # matters: the exception escapes `send` before the flag is set,
            # and it is precisely a write that failed partway through that may
            # have delivered a complete request. `sock` stays None when the
            # handshake itself never completed, so a connect-time failure (a
            # refused port, a dropped SYN) never marks the submission sent.
            if self.sock is not None:
                _SUBMITTED.set(True)


class _TrackedConnection(_MarksSubmission, http.client.HTTPConnection):
    pass


class _TrackedHTTPSConnection(_MarksSubmission, http.client.HTTPSConnection):
    pass


# Subclasses of the two default handlers, not extra ones. `build_opener`
# skips a default handler whose class a passed handler subclasses, so these
# replace them; added alongside, the defaults would sit earlier in the request
# chain and answer every call, and the tracking would never run.
class _TrackingHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_TrackedConnection, req)


class _TrackingHTTPSHandler(urllib.request.HTTPSHandler):
    # Set by `HTTPSHandler.__init__`; the stub does not declare it, and the
    # context it holds is the one that verifies the provider's certificate.
    _context: Any

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_TrackedHTTPSConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(_NoRedirects, _TrackingHTTPHandler, _TrackingHTTPSHandler)


def _declared_charset(headers: Any) -> str | None:
    """The charset a response's Content-Type declares, or None.

    `get_content_charset` is a method of `email.message.Message`, which is
    what urllib attaches to a response and to an `HTTPError` it built itself.
    It is looked up rather than called directly because a header mapping that
    is not a Message reaches here too (a proxy-shaped exception, a test
    double), and a refusal path must not raise `AttributeError` over the
    charset of a body it is about to describe.
    """
    get_charset = getattr(headers, "get_content_charset", None)
    return get_charset() if callable(get_charset) else None


def _decode_envelope(provider: str, raw: bytes, headers: Any) -> str:
    """The response body as text: the declared charset first, UTF-8 otherwise.

    JSON over HTTP defaults to UTF-8 (RFC 8259); a charset the provider
    actually declares in Content-Type wins when it decodes. Either way the
    decode is explicit and strict, so an invalid byte refuses the envelope
    naming the provider instead of raising a bare UnicodeDecodeError past
    this module's fault mapping (which would land after a billed submission)
    or silently substituting replacement characters into stored evidence.
    A byte-order mark is stripped on either path (`_without_bom`): the
    encoding the body declares about itself includes the mark, and the one
    character a JSON parser will not skip would otherwise turn a billed,
    perfectly good verdict into "returned a non-JSON envelope".
    """
    declared = _declared_charset(headers)
    if declared:
        try:
            return _without_bom(raw.decode(declared))
        except (UnicodeError, LookupError, ValueError):
            # Every failure mode a provider-declared charset can produce, each
            # with its own class: LookupError for a name this interpreter does
            # not know, UnicodeError (not just UnicodeDecodeError) for a decode
            # fault and for a codec such as `undefined` that raises the bare
            # form, and the plain ValueError `bytes.decode` raises for a name
            # carrying an embedded null. The header is provider-controlled, so
            # each must land in the refusal below rather than escape as a bare
            # traceback past this module's fault mapping after a billed
            # submission; an unusable declaration falls back to UTF-8.
            pass  # undecodable or unknown name: UTF-8 gets the next attempt
    try:
        return _without_bom(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        if declared:
            raise DeadeyeError(
                f"provider {provider!r} returned a body that decodes neither "
                f"as its declared charset {declared!r} nor as UTF-8: {exc}"
            ) from exc
        raise DeadeyeError(
            f"provider {provider!r} returned a body that is not valid UTF-8 "
            f"(JSON's default encoding): {exc}"
        ) from exc


def _read_chunk(response: Any, size: int) -> Any:
    """One socket read that returns as soon as any bytes have arrived.

    `HTTPResponse.read(n)` blocks until it has all `n` bytes or hits EOF, so
    one call can sit on a connection for as long as the provider keeps it
    open. `read1` stops at the first chunk the network delivered; a response
    object without it (a test double answering from memory) falls back to
    `read`, which bounds such a double by its own length anyway.
    """
    read1 = getattr(response, "read1", None)
    return read1(size) if callable(read1) else response.read(size)


def _read_response_body(
    response: Any, provider: str, *, deadline: float, timeout_seconds: float
) -> bytes:
    """Read one bounded, time-bounded successful JSON response.

    `deadline` is a monotonic instant the whole call is held to. urllib's
    `timeout=` is a per-socket-operation timeout, not a budget for the call: a
    provider that answers a byte every few seconds resets that clock on every
    read and the submission runs indefinitely, billing for as long as it keeps
    the connection open. Each read is already capped by the socket timeout, and
    the check between reads caps the sum, so the advertised "seconds to wait
    for the provider" is the real bound on the call.
    """
    chunks: list[bytes] = []
    remaining = _MAX_RESPONSE_BYTES + 1
    while remaining:
        if time.monotonic() >= deadline:
            raise did_not_answer(provider, timeout_seconds)
        raw = _read_chunk(response, min(64 * 1024, remaining))
        if not isinstance(raw, bytes):
            raise no_verdict(f"provider {provider!r} returned a non-bytes response body")
        if not raw:
            return b"".join(chunks)
        chunks.append(raw)
        remaining -= len(raw)
    raise no_verdict(
        f"provider {provider!r} returned more than {_MAX_RESPONSE_BYTES} response bytes; "
        "the review response is too large to retain safely"
    )


def _decode_fault_body(raw: bytes, headers: Any) -> str:
    """An error body as text, on the same rule the success path uses.

    A refusal line is the only account the operator gets of why a billed
    submission failed, so the declared charset has to be honoured here too:
    decoding a `charset=latin-1` error body as UTF-8 with `errors="replace"`
    turns every non-ASCII character in the provider's own explanation into
    U+FFFD, and two paths answering the same provider with two different
    decoders is how a mangled line stops being readable as a fault. The two
    differ only in what an unusable byte does, which the caller decides: a
    success body refuses, an error body replaces, because there is no second
    submission to protect and the line is better than nothing. A leading
    byte-order mark goes either way, for the reason `_without_bom` gives.
    """
    declared = _declared_charset(headers)
    if declared:
        try:
            return _without_bom(raw.decode(declared))
        except (UnicodeError, LookupError, ValueError):
            pass  # undecodable or unknown name: UTF-8 gets the next attempt
    return _without_bom(raw.decode("utf-8", errors="replace"))


def _read_fault_body(exc: urllib.error.HTTPError) -> str:
    """A bounded slice of an HTTP error body, then the socket is closed.

    The success path already caps what it retains; the error path must too.
    `HTTPError.read()` with no size would pull the whole body into memory
    (a hostile or misconfigured endpoint, or a proxy that answers with a
    media payload on 5xx) before the 300-character slice, and the MCP
    server is long-lived. Read only the character budget's worth of bytes,
    flatten them for stderr, and close so the connection is not pinned on
    the exception chain until the next GC pass.
    """
    chunks: list[bytes] = []
    # The budget is characters, so the read has to cover the longest byte
    # sequence one character can be. Reading the character count as bytes
    # instead cut a non-ASCII fault body at a fraction of the text an
    # operator asked for, and cut it mid-sequence, so the decoder's
    # `errors="replace"` spent the last characters of the line on a U+FFFD
    # standing in for the one the read severed.
    remaining = _MAX_FAULT_BODY_CHARS * _MAX_BYTES_PER_CHAR
    try:
        while remaining:
            raw = exc.read(min(64 * 1024, remaining))
            if not raw:
                break
            chunks.append(raw)
            remaining -= len(raw)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            exc.close()
    return flat_label_text(
        _decode_fault_body(b"".join(chunks), getattr(exc, "headers", None))[:_MAX_FAULT_BODY_CHARS]
    )


def post_json(
    provider: str,
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
    credential_env: str,
    credential: str,
) -> dict[str, Any]:
    """POST `body` as JSON to `url`, return the parsed JSON envelope.

    `url` is the adapter's fixed https API root (or an endpoint override
    already validated by `config.endpoint`) plus, at most, encoded model path
    segments: scheme and host are never caller-controlled. Redirects are never
    followed (`_NoRedirects`), so the credential cannot ride one elsewhere.
    `timeout_seconds` bounds the whole submission, response body included: the
    socket timeout is per operation, and the read loop carries the deadline
    that closes the gap.

    `credential` is the key the adapter put in `headers`, handed here so the
    fault path can scrub it back out of a body the endpoint chose.
    """
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    # One submission, one marker: the long-lived MCP server runs many reviews
    # in one context, and a leftover True from a previous one would mark the
    # next submission sent before it opened a socket.
    _SUBMITTED.set(False)
    # Monotonic, taken before the connection opens: the budget covers the
    # whole call, connect and response headers included. `timeout_seconds`
    # arms the socket, which bounds any single read that stalls; the deadline
    # bounds the sum of reads that keep making progress.
    deadline = time.monotonic() + timeout_seconds
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            raw = _read_response_body(
                response, provider, deadline=deadline, timeout_seconds=timeout_seconds
            )
            envelope: Any = loads(
                _decode_envelope(provider, raw, getattr(response, "headers", None))
            )
        if not isinstance(envelope, dict):
            # Valid JSON that is not an object (a bare array, a string) would
            # otherwise crash an adapter's key lookup with a raw traceback.
            # The request was answered, so the provider has it and may have
            # billed it: this is a spent submission, not a free retry.
            raise no_verdict(f"provider {provider!r} returned a non-object JSON envelope")
        # A `NaN` or `1e999` leaf a provider emitted would survive into
        # evidence, stdout, and MCP payloads no strict reader can parse; it
        # becomes null rather than refusing the whole envelope, because the
        # verdict text is billable and the usage metadata is not worth
        # discarding it over.
        return {key: strict_json_numbers(value) for key, value in envelope.items()}
    except urllib.error.HTTPError as exc:
        # A body that cannot be read must degrade to the status line, not
        # to an unbound name when the message below formats it. The read
        # is bounded, the socket is closed inside `_read_fault_body`, and
        # the credential is scrubbed out of whatever the endpoint echoed.
        detail = scrub_credential(_read_fault_body(exc), credential)
        if exc.code in _REDIRECT_CODES:
            raise DeadeyeError(
                f"provider {provider!r} answered with HTTP {exc.code} (redirect); "
                "deadeye never follows redirects because the provider credential "
                "must reach only the endpoint the request was addressed to"
            ) from exc
        if exc.code in (401, 403):
            raise DeadeyeError(
                f"provider {provider!r} rejected the credential (HTTP {exc.code}); "
                f"check the key in {credential_env} or config.local.toml"
            ) from exc
        if exc.code == 429:
            raise DeadeyeError(
                f"provider {provider!r} rate-limited or quota-exhausted the "
                f"request (HTTP 429): {detail}"
            ) from exc
        reason = f"provider {provider!r} refused the review (HTTP {exc.code}): {detail}"
        if exc.code >= 500:
            # A 4xx is the provider declining the request: the credential, the
            # quota, or the request itself was refused, no review ran, and the
            # key stays free for a corrected retry. A 5xx is the provider
            # reporting that *its* side broke, which it can do after the review
            # ran: the whole request, media included, was on the wire, and
            # nothing in the status says whether the attempt was billed before
            # the fault. That is the same ambiguity a timeout and a lost
            # connection carry, and both are already spent for exactly this
            # reason, so a 5xx is spent here too. Leaving it free tells a
            # deduplicating client the retry is safe, and that retry is a
            # second billable review of bytes the first attempt may already
            # have been charged for.
            reason = f"provider {provider!r} failed the review (HTTP {exc.code}): {detail}"
            raise no_verdict(reason) from exc
        raise DeadeyeError(reason) from exc
    except TimeoutError as exc:
        # The request may have reached the provider and completed there:
        # a caller that resubmits starts a second billable review, it does
        # not retry this one. Every ambiguous-outcome refusal says so.
        raise did_not_answer(provider, timeout_seconds) from exc
    except urllib.error.URLError as exc:
        if _SUBMITTED.get():
            # The socket was up and the request was on it, so the fault
            # arrived after the media left: a send that died partway, or a
            # handshake that completed and a timeout that fired while the body
            # was still going out. urllib reports that exactly as it reports a
            # host that was never reached, and the two differ where it costs
            # money: a plain fault leaves an idempotency key free, and a
            # client that retries a free key gets a second billable review of
            # bytes the provider may already have charged for. `NoVerdictError`
            # is what tells a deduplicating caller the key is spent.
            raise no_verdict(
                f"provider {provider!r} connection failed after the request was on "
                f"the wire, before any response arrived: {exc.reason}"
            ) from exc
        # The connection never came up: no name resolved, no port answered.
        # Nothing was submitted, so the refusal stays a plain one and the key
        # stays free for a corrected retry.
        raise DeadeyeError(
            f"provider {provider!r} could not be reached: {exc.reason}; no verdict was produced"
        ) from exc
    except json.JSONDecodeError as exc:
        # A 2xx body this tool cannot read is a completed generation, not a
        # refused request, so it is spent: the same type a timeout carries.
        raise no_verdict(f"provider {provider!r} returned a non-JSON envelope: {exc}") from exc
    except RecursionError as exc:
        # An envelope nested beyond the interpreter limit is a malformed
        # answer, not a fault here: refuse it like any other bad structure
        # (the same treatment parse_model_json and the MCP loop give theirs),
        # instead of letting the recursion escape as a raw traceback. The
        # provider answered, so it is a spent submission.
        raise no_verdict(
            f"provider {provider!r} returned an envelope nested too deeply to parse"
        ) from exc
    except (http.client.HTTPException, OSError) as exc:
        # A connection that dies mid-body (reset, truncated chunked
        # response) surfaces here, not as a traceback: the request was
        # billed and no verdict came back, which is a refusal to report.
        # The server side may still finish and bill the attempt, so the
        # refusal also warns against treating a resubmission as a retry,
        # and `NoVerdictError` lets a deduplicating caller record it as
        # spent instead of retrying into a second bill.
        raise NoVerdictError(
            f"provider {provider!r} connection failed before a complete "
            f"response arrived: {exc!r}; no verdict arrived, and the "
            "submission may still have completed and billed server-side: "
            "submitting again is a new billable review, not a retry of "
            "this one"
        ) from exc
