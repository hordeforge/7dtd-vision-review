"""NVIDIA NIM as a hosted vision adapter.

The OpenAI-compatible chat-completions endpoint
(`https://integrate.api.nvidia.com/v1/chat/completions`) accepts images as
`image_url` content parts; local frames are submitted as base64 data URLs, so
no upload round trip is needed. A second real provider behind the same narrow
protocol: bearer-token auth, no SDK, standard library only.

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

Media policy: this adapter takes images only (multi-image `image_url`
parts), never a muxed video — the sampling layer therefore always submits the
frame sequence, sampled down to the declared frame budget, which the
evidence records.
"""

from __future__ import annotations

import base64
import contextlib
import json
import urllib.error
import urllib.request

from .. import config
from ..errors import DeadeyeError
from .base import MediaPayload, ProviderLimits, ReviewRequest, ReviewResponse

API_ROOT = "https://integrate.api.nvidia.com/v1/chat/completions"
CREDENTIAL_ENV_VARS = ("NVIDIA_API_KEY",)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
# Conservative per-request budget: frames are far smaller, and the sampling
# layer caps the count before submission.
MAX_REQUEST_BYTES = 20 * 1024 * 1024
# Multi-image vision-chat limits sit well below a 10s/4fps clip's 40 frames,
# so the sampling layer drops to this with even spacing, first and last kept.
MAX_FRAMES_PER_REQUEST = 16

DEFAULT_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
DEFAULT_MAX_TOKENS = 65536
DEFAULT_REASONING_BUDGET = 16384
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.95

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


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
            suffixes=IMAGE_SUFFIXES,
            max_bytes=MAX_REQUEST_BYTES,
            max_frames=MAX_FRAMES_PER_REQUEST,
            accepts_video=False,
            max_video_bytes=None,
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
        endpoint = config.value(("providers", "nvidia", "endpoint"))
        api_root = endpoint if isinstance(endpoint, str) and endpoint else API_ROOT
        # Both audited statements carry the same justification: the URL is
        # this module's fixed https constant (or the config override); scheme
        # and host are never caller-controlled.
        http_request = urllib.request.Request(  # noqa: S310
            api_root,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Header, not query parameter: the key must never appear in a URL.
                "Authorization": f"Bearer {credential}",
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
                    f"provider 'nvidia' rejected the credential (HTTP {exc.code}); "
                    "check the key in NVIDIA_API_KEY or config.local.toml"
                ) from exc
            if exc.code == 429:
                raise DeadeyeError(
                    f"provider 'nvidia' rate-limited or quota-exhausted the request "
                    f"(HTTP 429): {detail}"
                ) from exc
            raise DeadeyeError(
                f"provider 'nvidia' refused the review (HTTP {exc.code}): {detail}"
            ) from exc
        except TimeoutError as exc:
            raise DeadeyeError(
                f"provider 'nvidia' did not answer within {request.timeout_seconds:g}s; "
                "no verdict was produced"
            ) from exc
        except urllib.error.URLError as exc:
            raise DeadeyeError(
                f"provider 'nvidia' could not be reached: {exc.reason}; no verdict was produced"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DeadeyeError(f"provider 'nvidia' returned a non-JSON envelope: {exc}") from exc

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

    Local frames travel as base64 data URLs in `image_url` parts, addressed
    from the text side by the same fixed attachment labels the prompt
    announces.
    """
    parts: list[dict[str, object]] = [{"type": "text", "text": request.prompt}]
    for payload in request.media:
        parts.append({"type": "text", "text": _label_for(payload)})
        if not payload.mime_type.startswith("image/"):
            raise DeadeyeError(
                f"provider 'nvidia' cannot ingest {payload.mime_type}; it is a "
                "vision-chat endpoint that takes images only, so a muxed video "
                "cannot reach it (the sampling layer sends frames instead)"
            )
        data_url = f"data:{payload.mime_type};base64," + base64.b64encode(payload.data).decode(
            "ascii"
        )
        parts.append({"type": "image_url", "image_url": {"url": data_url}})
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


def _label_for(payload: MediaPayload) -> str:
    if payload.kind == "reference":
        return f"reference image: {payload.name}"
    return f"frame attachment: {payload.name}"
