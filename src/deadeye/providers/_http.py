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
    segments: scheme and host are never caller-controlled.
    """
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=timeout_seconds
        ) as response:
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
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        with contextlib.suppress(OSError):
            exc.close()
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
