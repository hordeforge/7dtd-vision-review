"""Shared stdlib HTTP submission for the hosted adapters.

Both adapters POST one JSON document and read one JSON envelope back, and
every fault maps to one DeadeyeError naming the provider. A timeout or a
mid-body connection failure may still have completed and billed server-side,
so those refusals say so explicitly: submitting again is a new billable
review, never a retry.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..errors import DeadeyeError, NoVerdictError, did_not_answer, no_verdict
from ..json_safe import strict_json_numbers
from ..prompt_text import flat_label_text

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# How much of a provider's error body may ride in a refusal line: enough to
# name the fault (quota, malformed key) and never a whole payload.
_MAX_FAULT_BODY_CHARS = 300
# A successful model response is a compact JSON verdict, not a media stream.
# Bound it so a malformed endpoint or proxy cannot make the long-lived MCP
# server retain an unbounded response body. Eight MiB leaves ample room for a
# 65k-token JSON verdict plus provider metadata.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


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
        newurl: str,
    ) -> urllib.request.Request:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirects)


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
    """
    declared = _declared_charset(headers)
    if declared:
        try:
            return raw.decode(declared)
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
        return raw.decode("utf-8")
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
    submission to protect and the line is better than nothing.
    """
    declared = _declared_charset(headers)
    if declared:
        try:
            return raw.decode(declared)
        except (UnicodeError, LookupError, ValueError):
            pass  # undecodable or unknown name: UTF-8 gets the next attempt
    return raw.decode("utf-8", errors="replace")


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
    remaining = _MAX_FAULT_BODY_CHARS
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
) -> dict[str, Any]:
    """POST `body` as JSON to `url`, return the parsed JSON envelope.

    `url` is the adapter's fixed https API root (or an endpoint override
    already validated by `config.endpoint`) plus, at most, encoded model path
    segments: scheme and host are never caller-controlled. Redirects are never
    followed (`_NoRedirects`), so the credential cannot ride one elsewhere.
    `timeout_seconds` bounds the whole submission, response body included: the
    socket timeout is per operation, and the read loop carries the deadline
    that closes the gap.
    """
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
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
            envelope: Any = json.loads(
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
        # is bounded and the socket is closed inside `_read_fault_body`.
        detail = _read_fault_body(exc)
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
        raise DeadeyeError(
            f"provider {provider!r} refused the review (HTTP {exc.code}): {detail}"
        ) from exc
    except TimeoutError as exc:
        # The request may have reached the provider and completed there:
        # a caller that resubmits starts a second billable review, it does
        # not retry this one. Every ambiguous-outcome refusal says so.
        raise did_not_answer(provider, timeout_seconds) from exc
    except urllib.error.URLError as exc:
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
