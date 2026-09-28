"""Google's Gemini as the first hosted video adapter.

Chosen for the same reasons the sibling audio pipeline chose it: the API
accepts video and multi-image input inline (base64, no upload round trip), can
be asked for JSON output, and needs only the standard library to reach — no
SDK, no new dependency for a consuming mod author to audit. The model
identifier is a default, not a contract: providers and model names change, so
the caller can always pass `--model` and `deadeye doctor` reports
configuration rather than hard-coding one vendor.

The key arrives from `GEMINI_API_KEY` / `GOOGLE_API_KEY` (environment) or
`providers.gemini.api_key` in the loaded configuration (normally the
gitignored `config.local.toml`; see `config.py` for precedence), is sent in a
header (never a query string, so it cannot land in an access log), and is
never printed, logged, or written into evidence.

Media policy: a muxed video goes inline when it fits the per-request inline
budget; otherwise (or when the clip has no muxed video) the sampled frame
sequence goes as multi-image input. The `video/mp4` inline path is the
documented route for video understanding; multi-image input is the broadly
supported fallback every vision-chat API shares.

The reviewer instruction travels as `systemInstruction` and the authored
intent as the user turn, never concatenated: the roles keep intent text out
of the slot the instruction occupies.
"""

from __future__ import annotations

import base64
import urllib.parse

from .. import config
from ..errors import DeadeyeError
from ..result import BASE_RUBRIC, RESULT_KEYS
from ..sampling import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from ._http import post_json
from .base import (
    ProviderLimits,
    ReviewRequest,
    ReviewResponse,
    attachment_label,
    first_response_object,
    float_setting,
    int_setting,
    response_object,
)

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
# A single sample, not a tuning knob that was left alone: the 2.5 series
# defaults to temperature 1.0, and a review is a judgment call meant to be
# traceable to the submission, not a draw from a wide distribution. Naming
# the sampling parameters puts them in the request the evidence accounts for
# instead of leaving them to a provider default that can move server-side
# without a version bump. A deployment overrides it under
# `providers.gemini.temperature`.
DEFAULT_TEMPERATURE = 0.2
# A default, not a contract: a deployment overrides it with
# `providers.gemini.model` or `--model`, exactly as for the other providers.
DEFAULT_MODEL = "gemini-2.5-flash"

# The verdict shape, as a `responseSchema`, so the decoder is held to the
# contract the reviewer instruction states in prose.
#
# The prose in `prompt.py` lets a moment be named either way (`[start, end]`
# or a single number); the OpenAPI subset Gemini accepts has no union type, so
# this names the single-number form, which is the intersection of what the
# instruction permits and what `validate_result` accepts. Every key the
# validator requires is listed and marked required, and the rubric dimensions
# are spelled out from `BASE_RUBRIC`, so "score every dimension listed; score
# nothing that is not listed" is enforced by the decoder rather than left to a
# refusal after the submission has been billed. A model that would otherwise
# answer in prose, or with a wrapped key the prompt never mentioned, now
# cannot: the shape is constrained before generation starts.
_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "at_seconds": {"type": "NUMBER", "nullable": True},
                    "at_frame": {"type": "NUMBER", "nullable": True},
                },
                "required": ["description"],
            },
        },
        "recommended_changes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "rubric_scores": {
            "type": "OBJECT",
            "properties": {
                dimension.key: {"type": "NUMBER", "nullable": True} for dimension in BASE_RUBRIC
            },
        },
        "confidence": {"type": "NUMBER"},
        "limitations": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": list(RESULT_KEYS),
}


class GeminiProvider:
    name = "gemini"
    endpoint_mode = "hosted-api:inline-base64"
    requires_credential = True
    credential_env_names = CREDENTIAL_ENV_VARS

    @property
    def default_model(self) -> str:
        return config.text(("providers", "gemini", "model")) or DEFAULT_MODEL

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
        """The configured key (environment first, then configuration), or None.

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
        body = build_body(request, provider_name=self.name)
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

        candidates = envelope.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            feedback = envelope.get("promptFeedback")
            reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
            raise DeadeyeError(
                "provider 'gemini' returned no candidate"
                + (f" (blocked: {reason})" if reason else "")
                + "; no verdict was produced"
            )
        candidate = first_response_object(
            envelope, key="candidates", item_name="candidate", provider_name=self.name
        )
        content = response_object(
            candidate, key="content", item_name="candidate", provider_name=self.name
        )
        raw_parts = content.get("parts", [])
        if not isinstance(raw_parts, list):
            raise DeadeyeError(
                "provider 'gemini' returned invalid candidate parts; no verdict was produced"
            )
        text = "".join(
            part["text"]
            for part in raw_parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        finish = candidate.get("finishReason")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            raise DeadeyeError(
                f"provider 'gemini' ended the response early (finishReason {finish}); "
                "no verdict was produced"
            )
        usage = envelope.get("usageMetadata")
        return ReviewResponse(
            raw_text=text,
            usage=usage if isinstance(usage, dict) else None,
            model_reported=(
                envelope["modelVersion"] if isinstance(envelope.get("modelVersion"), str) else None
            ),
        )


def build_body(request: ReviewRequest, *, provider_name: str = "gemini") -> dict[str, object]:
    """The `generateContent` payload, as a plain dict (offline-testable).

    The reviewer instruction rides `systemInstruction`, its own role in the
    API, and only the authored intent and the attachments occupy the user
    turn. Gemini gives a `systemInstruction` the standing the OpenAI-shaped
    `system` message does elsewhere; keeping them apart is what stops intent
    text from being read as instruction.

    The verdict shape travels as `responseSchema` beside the JSON mime type, so
    the decoder is constrained to the keys the instruction names rather than
    asked for them in prose and refused when it misses.
    """
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
    body: dict[str, object] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            # A cap, not a tuning knob: an uncapped generation is unbounded
            # spend when the model loops. Override with
            # providers.gemini.max_output_tokens.
            "maxOutputTokens": int_setting(
                provider_name, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS, minimum=1
            ),
            "temperature": float_setting(provider_name, "temperature", DEFAULT_TEMPERATURE),
        },
    }
    if request.system_prompt:
        body["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
    return body
