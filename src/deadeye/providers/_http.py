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
import urllib.error
import urllib.request
from typing import Any

from ..errors import DeadeyeError

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# How much of a provider's error body may ride in a refusal line: enough to
# name the fault (quota, malformed key) and never a whole payload.
_MAX_FAULT_BODY_CHARS = 300


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
            envelope: dict[str, Any] = json.load(response)
        return envelope
    except urllib.error.HTTPError as exc:
        # A body that cannot be read must degrade to the status line, not
        # to an unbound name when the message below formats it. The error
        # body owns the request's socket until closed, so close it here
        # rather than leaving it to the cyclic collector: the MCP server
        # is long-lived, and each refused review would otherwise hold one
        # dead connection until a GC pass reclaims the exception chain.
        detail = ""
        with contextlib.suppress(OSError):
            detail = exc.read().decode("utf-8", errors="replace")[:_MAX_FAULT_BODY_CHARS]
        with contextlib.suppress(OSError):
            exc.close()
        # The body is provider-controlled text that lands in stderr lines;
        # flatten control characters so one response cannot forge extra
        # disclosure-shaped lines in an operator's transcript.
        detail = "".join(char if char.isprintable() else " " for char in detail)
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
