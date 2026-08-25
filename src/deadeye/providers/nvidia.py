"""NVIDIA NIM as a hosted vision adapter.

The OpenAI-compatible chat-completions endpoint
(`https://integrate.api.nvidia.com/v1/chat/completions`) accepts images as
`image_url` content parts and videos as `video_url` parts (the omni model is
video-capable, per NVIDIA's own API reference: "Videos use type =
video_url"); local media is submitted as base64 data URLs, so no upload round
trip is needed. A second real provider behind the same narrow protocol:
bearer-token auth, no SDK, standard library only.

The model identifier is a default, not a contract: providers and model names
change, so the caller can always pass `--model`. The generation defaults
(`max_tokens`, `reasoning_budget`, `temperature`, `top_p`) mirror the
verified payload for `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`; they
are module constants because they are the model's own tuning surface, not
per-review knobs.

The key arrives from `NVIDIA_API_KEY` (environment) or
`providers.nvidia.api_key` in `config.local.toml` (see `config.py` for the
precedence), travels in an `Authorization` header (never a query string, so
it cannot land in an access log), and is never printed, logged, or written
into evidence.

Media policy: a muxed video goes as a single `video_url` part when one exists
and fits the inline budget; otherwise the frame sequence goes as multi-image
`image_url` parts, sampled down to the declared frame budget (the API's
verified 12-image cap), which the evidence records.
"""

from __future__ import annotations

import base64

from .. import config
from ..errors import DeadeyeError
from ..sampling import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from ._http import post_json
from .base import ProviderLimits, ReviewRequest, ReviewResponse, attachment_label

API_ROOT = "https://integrate.api.nvidia.com/v1/chat/completions"
CREDENTIAL_ENV_VARS = ("NVIDIA_API_KEY",)
# Conservative per-request budget: media is far smaller, and the sampling
# layer caps the count before submission.
MAX_REQUEST_BYTES = 20 * 1024 * 1024
# Multi-image vision-chat limits sit well below a 10s/4fps clip's 40 frames,
# so the sampling layer drops to this with even spacing, first and last kept.
# 12 is the API's own published bound, verified live: a 16-frame submission
# was refused with "At most 12 image(s) may be provided in one prompt".
# A single video part is not subject to the image cap.
MAX_FRAMES_PER_REQUEST = 12

DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
DEFAULT_MAX_TOKENS = 65536
DEFAULT_REASONING_BUDGET = 16384
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.95


class NvidiaProvider:
    name = "nvidia"
    endpoint_mode = "hosted-api:openai-compatible-chat"
    requires_credential = True
    credential_env_names = CREDENTIAL_ENV_VARS

    @property
    def default_model(self) -> str:
        configured = config.value(("providers", "nvidia", "model"))
        return configured if isinstance(configured, str) and configured else DEFAULT_MODEL

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
        return config.credential_for("nvidia", CREDENTIAL_ENV_VARS)

    def is_configured(self) -> bool:
        return self.credential() is not None

    def configuration_hint(self) -> str:
        return (
            f"set {CREDENTIAL_ENV_VARS[0]} or put api_key under [providers.nvidia] "
            "in config.local.toml; create a key at https://build.nvidia.com"
        )

    def review(self, request: ReviewRequest) -> ReviewResponse:
        credential = self.credential()
        if credential is None:
            raise DeadeyeError(f"provider 'nvidia' has no credential; {self.configuration_hint()}")
        body = build_body(request)
        # The override is validated in config.endpoint: https only, except a
        # loopback proxy over plain http.
        api_root = config.endpoint(("providers", "nvidia", "endpoint"), API_ROOT)
        envelope = post_json(
            self.name,
            api_root,
            body=body,
            headers={
                "Accept": "application/json",
                # Header, not query parameter: the key must never appear in a URL.
                "Authorization": f"Bearer {credential}",
            },
            timeout_seconds=request.timeout_seconds,
            credential_env=CREDENTIAL_ENV_VARS[0],
        )

        choices = envelope.get("choices") or []
        if not choices:
            raise DeadeyeError("provider 'nvidia' returned no choice; no verdict was produced")
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise DeadeyeError(
                "provider 'nvidia' returned no text content; no verdict was produced"
            )
        finish = choices[0].get("finish_reason")
        if finish and finish not in ("stop", "length"):
            raise DeadeyeError(
                f"provider 'nvidia' ended the response early (finish_reason {finish}); "
                "no verdict was produced"
            )
        usage = envelope.get("usage")
        return ReviewResponse(
            raw_text=text,
            usage=usage if isinstance(usage, dict) else None,
            model_reported=envelope.get("model"),
        )


def build_body(request: ReviewRequest) -> dict[str, object]:
    """The chat-completions payload, as a plain dict (offline-testable).

    Local media travels as base64 data URLs: frames in `image_url` parts, a
    muxed video in a single `video_url` part (NVIDIA's documented form for
    video in chat completions), addressed from the text side by the same fixed
    attachment labels the prompt announces.
    """
    parts: list[dict[str, object]] = [{"type": "text", "text": request.prompt}]
    for payload in request.media:
        parts.append({"type": "text", "text": attachment_label(payload)})
        data_url = f"data:{payload.mime_type};base64," + base64.b64encode(payload.data).decode(
            "ascii"
        )
        if payload.mime_type.startswith("video/"):
            parts.append({"type": "video_url", "video_url": {"url": data_url}})
        elif payload.mime_type.startswith("image/"):
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            raise DeadeyeError(
                f"provider 'nvidia' cannot ingest {payload.mime_type}; it is a "
                "vision-chat endpoint that takes images and video only"
            )
    return {
        "messages": [{"role": "user", "content": parts}],
        "model": request.model,
        "max_tokens": _int_config("max_tokens", DEFAULT_MAX_TOKENS),
        "reasoning_budget": _int_config("reasoning_budget", DEFAULT_REASONING_BUDGET),
        "temperature": _float_config("temperature", DEFAULT_TEMPERATURE),
        "top_p": _float_config("top_p", DEFAULT_TOP_P),
        "stream": False,
    }


def _int_config(key: str, fallback: int) -> int:
    value = config.value(("providers", "nvidia", key))
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _float_config(key: str, fallback: float) -> float:
    value = config.value(("providers", "nvidia", key))
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback
