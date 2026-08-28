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
import math
import urllib.error
import urllib.request
from typing import Any

from ..errors import DeadeyeError
from ..sampling import flat_label_text

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


def _strict_json_numbers(value: Any) -> Any:
    """Neutralize `NaN`/`Infinity` leaves a provider emitted.

    Python's JSON parser accepts those bare tokens although RFC 8259 does
    not, and an extreme exponent (`1e999`) silently parses to infinity. Left
    in place they would survive into evidence, stdout, and MCP payloads that
    no strict reader can parse. They become null instead of refusing the
    whole envelope: the verdict text is billable, usage metadata is not worth
    discarding it over. Finite numbers pass through untouched.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _strict_json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strict_json_numbers(item) for item in value]
    return value


def _decode_envelope(provider: str, raw: bytes, headers: Any) -> str:
    """The response body as text: the declared charset first, UTF-8 otherwise.

    JSON over HTTP defaults to UTF-8 (RFC 8259); a charset the provider
    actually declares in Content-Type wins when it decodes. Either way the
    decode is explicit and strict, so an invalid byte refuses the envelope
    naming the provider instead of raising a bare UnicodeDecodeError past
    this module's fault mapping (which would land after a billed submission)
    or silently substituting replacement characters into stored evidence.
    """
    declared = headers.get_content_charset() if headers is not None else None
    if declared:
        try:
            return raw.decode(declared)
        except (UnicodeDecodeError, LookupError):
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


def _read_response_body(response: Any, provider: str) -> bytes:
    """Read one bounded successful JSON response from a hosted provider."""
    chunks: list[bytes] = []
    remaining = _MAX_RESPONSE_BYTES + 1
    while remaining:
        raw = response.read(min(64 * 1024, remaining))
        if not isinstance(raw, bytes):
            raise DeadeyeError(f"provider {provider!r} returned a non-bytes response body")
        if not raw:
            return b"".join(chunks)
        chunks.append(raw)
        remaining -= len(raw)
    raise DeadeyeError(
        f"provider {provider!r} returned more than {_MAX_RESPONSE_BYTES} response bytes; "
        "the review response is too large to retain safely"
    )


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
        b"".join(chunks).decode("utf-8", errors="replace")[:_MAX_FAULT_BODY_CHARS]
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
    """
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            raw = _read_response_body(response, provider)
            envelope: Any = json.loads(
                _decode_envelope(provider, raw, getattr(response, "headers", None))
            )
        if not isinstance(envelope, dict):
            # Valid JSON that is not an object (a bare array, a string) would
            # otherwise crash an adapter's key lookup with a raw traceback.
            raise DeadeyeError(f"provider {provider!r} returned a non-object JSON envelope")
        return {key: _strict_json_numbers(value) for key, value in envelope.items()}
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
        raise DeadeyeError(
            f"provider {provider!r} did not answer within {timeout_seconds:g}s; "
            "no verdict arrived, and the submission may still have completed "
            "and billed server-side: submitting again is a new billable "
            "review, not a retry of this one"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeadeyeError(
            f"provider {provider!r} could not be reached: {exc.reason}; no verdict was produced"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DeadeyeError(f"provider {provider!r} returned a non-JSON envelope: {exc}") from exc
    except RecursionError as exc:
        # An envelope nested beyond the interpreter limit is a malformed
        # answer, not a fault here: refuse it like any other bad structure
        # (the same treatment parse_model_json and the MCP loop give theirs),
        # instead of letting the recursion escape as a raw traceback.
        raise DeadeyeError(
            f"provider {provider!r} returned an envelope nested too deeply to parse"
        ) from exc
    except (http.client.HTTPException, OSError) as exc:
        # A connection that dies mid-body (reset, truncated chunked
        # response) surfaces here, not as a traceback: the request was
        # billed and no verdict came back, which is a refusal to report.
        # The server side may still finish and bill the attempt, so the
        # refusal also warns against treating a resubmission as a retry.
        raise DeadeyeError(
            f"provider {provider!r} connection failed before a complete "
            f"response arrived: {exc!r}; no verdict arrived, and the "
            "submission may still have completed and billed server-side: "
            "submitting again is a new billable review, not a retry of "
            "this one"
        ) from exc
