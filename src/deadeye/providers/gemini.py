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
import urllib.parse

from .. import config
from ..errors import DeadeyeError
from ..sampling import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from ._http import post_json
from .base import ProviderLimits, ReviewRequest, ReviewResponse, attachment_label, int_setting

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
CREDENTIAL_ENV_VARS: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# Gemini documents inline data for both images and video; the ~20 MB figure is
# the published per-request budget for inline media. Frames are far smaller,
# and the sampling layer caps the count before submission.
MAX_REQUEST_BYTES = 20 * 1024 * 1024
MAX_FRAMES_PER_REQUEST = 40
# The result shape is small, but the model also spends thinking tokens inside
# this budget on the 2.5 series, so it stays at the published ceiling rather
# than a tight cap: its job is to stop a runaway or looping generation from
# billing without end, not to truncate an honest verdict mid-JSON.
DEFAULT_MAX_OUTPUT_TOKENS = 65536


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
            label = attachment_label(payload)
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
            "generationConfig": {
                "response_mime_type": "application/json",
                # A cap, not a tuning knob: an uncapped generation is unbounded
                # spend when the model loops. Override with
                # providers.gemini.max_output_tokens.
                "maxOutputTokens": int_setting(
                    self.name, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
            },
        }
        # The override is validated in config.endpoint: https only, except a
        # loopback proxy over plain http.
        api_root = config.endpoint(("providers", "gemini", "endpoint"), API_ROOT)
        envelope = post_json(
            self.name,
            # The model is one path segment and must be percent-encoded: a
            # name with a space or non-ASCII character would otherwise be
            # sent as raw latin-1 request-line bytes (mojibake) or fail the
            # ASCII encode.
            f"{api_root}/{urllib.parse.quote(request.model, safe='')}:generateContent",
            body=body,
            headers={
                # Header, not query parameter: the key must never appear in a URL.
                "x-goog-api-key": credential,
            },
            timeout_seconds=request.timeout_seconds,
            credential_env=CREDENTIAL_ENV_VARS[0],
        )

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
