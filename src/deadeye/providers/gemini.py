"""Google's Gemini as the first hosted video adapter.

Chosen for the same reasons the sibling audio pipeline chose it: the API
accepts video and multi-image input inline (base64, no upload round trip), can
be asked for JSON output, and needs only the standard library to reach — no
SDK, no new dependency for a consuming mod author to audit. The model
identifier is a default, not a contract: providers and model names change, so
the caller can always pass `--model` and `deadeye doctor` reports
configuration rather than hard-coding one vendor.

The key arrives from `GEMINI_API_KEY` / `GOOGLE_API_KEY` (environment) or
`providers.gemini.api_key` in `config.local.toml` (see `config.py` for the
precedence), is sent in a header (never a query string, so it cannot land in
an access log), and is never printed, logged, or written into evidence.

Media policy: a muxed video goes inline when it fits the per-request inline
budget; otherwise (or when the clip has no muxed video) the sampled frame
sequence goes as multi-image input. The `video/mp4` inline path is the
documented route for video understanding; multi-image input is the broadly
supported fallback every vision-chat API shares.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import urllib.error
import urllib.request

from .. import config
from ..errors import DeadeyeError
from .base import MediaPayload, ProviderLimits, ReviewRequest, ReviewResponse

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
CREDENTIAL_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# Gemini documents inline data for both images and video; the ~20 MB figure is
# the published per-request budget for inline media. Frames are far smaller,
# and the sampling layer caps the count before submission.
MAX_REQUEST_BYTES = 20 * 1024 * 1024
MAX_FRAMES_PER_REQUEST = 40
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


class GeminiProvider:
    name = "gemini"
    endpoint_mode = "hosted-api:inline-base64"
    requires_credential = True
    credential_env_names = CREDENTIAL_ENV_VARS

    @property
    def default_model(self) -> str:
        configured = config.value(("providers", "gemini", "model"))
        return configured if isinstance(configured, str) and configured else "gemini-2.5-flash"

    @property
    def limits(self) -> ProviderLimits:
        return ProviderLimits(
            suffixes=IMAGE_SUFFIXES + VIDEO_SUFFIXES,
            max_bytes=MAX_REQUEST_BYTES,
            max_frames=MAX_FRAMES_PER_REQUEST,
            accepts_video=True,
            max_video_bytes=MAX_REQUEST_BYTES,
        )

    def credential(self) -> str | None:
        """The configured key (env first, then config.local.toml), or None.

        Never logged; callers send it only.
        """
        return config.credential_for("gemini", CREDENTIAL_ENV_VARS)

    def is_configured(self) -> bool:
        return self.credential() is not None

    def configuration_hint(self) -> str:
        return (
            f"set {CREDENTIAL_ENV_VARS[0]} or put api_key under [providers.gemini] "
            "in config.local.toml; create a key at https://aistudio.google.com/apikey"
        )

    def review(self, request: ReviewRequest) -> ReviewResponse:
        credential = self.credential()
        if credential is None:
            raise DeadeyeError(f"provider 'gemini' has no credential; {self.configuration_hint()}")
        parts: list[dict[str, object]] = [{"text": request.prompt}]
        for payload in request.media:
            label = _label_for(payload)
            parts.append({"text": label})
            parts.append(
                {
                    "inline_data": {
                        "mime_type": payload.mime_type,
                        "data": base64.b64encode(payload.data).decode("ascii"),
                    }
                }
            )
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"response_mime_type": "application/json"},
        }
        # The override is validated in config.endpoint: https only, except a
        # loopback proxy over plain http.
        api_root = config.endpoint(("providers", "gemini", "endpoint"), API_ROOT)
        # Both audited statements carry the same justification: the URL is
        # this module's fixed https constant (or the config override) plus
        # the requested model name; scheme and host are never caller-controlled.
        http_request = urllib.request.Request(  # noqa: S310
            f"{api_root}/{request.model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Header, not query parameter: the key must never appear in a URL.
                "x-goog-api-key": credential,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                http_request, timeout=request.timeout_seconds
            ) as response:
                envelope = json.load(response)
        except urllib.error.HTTPError as exc:
            # A body that cannot be read must degrade to the status line, not
            # to an unbound name when the message below formats it.
            detail = ""
            with contextlib.suppress(OSError):
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in (401, 403):
                raise DeadeyeError(
                    f"provider 'gemini' rejected the credential (HTTP {exc.code}); "
                    "check the key in GEMINI_API_KEY or config.local.toml"
                ) from exc
            if exc.code == 429:
                raise DeadeyeError(
                    f"provider 'gemini' rate-limited or quota-exhausted the request "
                    f"(HTTP 429): {detail}"
                ) from exc
            raise DeadeyeError(
                f"provider 'gemini' refused the review (HTTP {exc.code}): {detail}"
            ) from exc
        except TimeoutError as exc:
            raise DeadeyeError(
                f"provider 'gemini' did not answer within {request.timeout_seconds:g}s; "
                "no verdict was produced"
            ) from exc
        except urllib.error.URLError as exc:
            raise DeadeyeError(
                f"provider 'gemini' could not be reached: {exc.reason}; no verdict was produced"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DeadeyeError(f"provider 'gemini' returned a non-JSON envelope: {exc}") from exc
        except (http.client.HTTPException, OSError) as exc:
            # A connection that dies mid-body (reset, truncated chunked
            # response) surfaces here, not as a traceback: the request was
            # billed and no verdict came back, which is a refusal to report.
            raise DeadeyeError(
                f"provider 'gemini' connection failed before a complete "
                f"response arrived: {exc!r}; no verdict was produced"
            ) from exc

        candidates = envelope.get("candidates") or []
        if not candidates:
            feedback = envelope.get("promptFeedback") or {}
            reason = feedback.get("blockReason")
            raise DeadeyeError(
                "provider 'gemini' returned no candidate"
                + (f" (blocked: {reason})" if reason else "")
                + "; no verdict was produced"
            )
        content = candidates[0].get("content") or {}
        text = "".join(
            part.get("text", "") for part in content.get("parts", []) if isinstance(part, dict)
        )
        finish = candidates[0].get("finishReason")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            raise DeadeyeError(
                f"provider 'gemini' ended the response early (finishReason {finish}); "
                "no verdict was produced"
            )
        usage = envelope.get("usageMetadata")
        return ReviewResponse(
            raw_text=text,
            usage=usage if isinstance(usage, dict) else None,
            model_reported=envelope.get("modelVersion"),
        )


def _label_for(payload: MediaPayload) -> str:
    if payload.kind == "video":
        return f"video attachment: {payload.name}"
    if payload.kind == "reference":
        return f"reference image: {payload.name}"
    return f"frame attachment: {payload.name}"
