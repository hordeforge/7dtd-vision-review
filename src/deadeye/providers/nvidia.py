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
are module constants read through the `int_setting` / `float_setting` readers
in `base.py`, so a deployment overrides any of them under
`[providers.nvidia]` and a review is never sent parameters its evidence cannot
account for.

The key arrives from `NVIDIA_API_KEY` (environment) or
`providers.nvidia.api_key` in the loaded configuration (normally the
gitignored `config.local.toml`; see `config.py` for precedence), travels in
an `Authorization` header (never a query string, so it cannot land in an
access log), and is never printed, logged, or written into evidence.

Media policy: a muxed video goes as a single `video_url` part when one exists
and fits the inline budget; otherwise the frame sequence goes as multi-image
`image_url` parts, sampled down to the declared frame budget (the API's
verified 12-image cap), which the evidence records.
"""

from __future__ import annotations

import base64

from .. import config
from ..errors import DeadeyeError, NoVerdictError, no_verdict
from ._http import post_json
from .base import (
    CredentialedProvider,
    ReviewRequest,
    ReviewResponse,
    attachment_label,
    first_response_object,
    float_setting,
    int_setting,
    response_object,
)

API_ROOT = "https://integrate.api.nvidia.com/v1/chat/completions"
CREDENTIAL_ENV_VARS: tuple[str, ...] = ("NVIDIA_API_KEY",)
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
# The ranges the chat-completions API documents for these two knobs. A
# configured value outside them is refused before submission rather than
# billed and rejected by the endpoint.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_TOP_P = 0.0
MAX_TOP_P = 1.0


def generation_settings() -> dict[str, object]:
    """The generation parameters one submission sends, resolved from configuration.

    Read once per submission: the core resolves it before building the request,
    `build_body` sends the mapping it was handed, and the evidence envelope
    records that same mapping. Two reads of the same configuration are two
    answers to one question, and the config cache reloads on a source-file
    change, so the second could differ from the request that was sent.
    """
    return {
        "max_tokens": int_setting("nvidia", "max_tokens", DEFAULT_MAX_TOKENS, minimum=1),
        "reasoning_budget": int_setting("nvidia", "reasoning_budget", DEFAULT_REASONING_BUDGET),
        "temperature": float_setting(
            "nvidia",
            "temperature",
            DEFAULT_TEMPERATURE,
            minimum=MIN_TEMPERATURE,
            maximum=MAX_TEMPERATURE,
        ),
        "top_p": float_setting(
            "nvidia", "top_p", DEFAULT_TOP_P, minimum=MIN_TOP_P, maximum=MAX_TOP_P
        ),
    }


class NvidiaProvider(CredentialedProvider):
    name = "nvidia"
    endpoint_mode = "hosted-api:openai-compatible-chat"
    credential_env_names = CREDENTIAL_ENV_VARS
    default_model_name = DEFAULT_MODEL
    max_request_bytes = MAX_REQUEST_BYTES
    max_frames_per_request = MAX_FRAMES_PER_REQUEST

    def configuration_hint(self) -> str:
        return (
            f"set {CREDENTIAL_ENV_VARS[0]} or put api_key under [providers.nvidia] "
            "in config.local.toml; create a key at https://build.nvidia.com"
        )

    def generation_settings(self) -> dict[str, object]:
        return generation_settings()

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
            credential=credential,
        )

        choice = first_response_object(
            envelope, key="choices", item_name="choice", provider_name=self.name
        )
        message = response_object(
            choice, key="message", item_name="choice", provider_name=self.name
        )
        text = _answer_text(message.get("content"))
        finish = choice.get("finish_reason")
        if finish and finish not in ("stop", "length"):
            raise no_verdict(
                f"provider {self.name!r} ended the response early (finish_reason {finish})"
            )
        if not text:
            # Named here rather than left to the result parser: an empty
            # message reaches `parse_model_json` as `""`, whose JSONDecodeError
            # reads as "invalid structure (not JSON): Expecting value: line 1
            # column 1" and says nothing about the provider having sent no text
            # at all. A generation stopped at the token cap is the usual cause,
            # and it is a setting the operator can change, so the knob is
            # named as it is in the Gemini adapter.
            raise NoVerdictError(
                f"provider {self.name!r} returned no text content"
                + (f" (finish_reason {finish})" if finish else "")
                + "; no verdict was produced"
                + (
                    "; raise providers.nvidia.max_tokens if the generation was "
                    "cut short by the token cap"
                    if finish == "length"
                    else ""
                )
            )
        usage = envelope.get("usage")
        return ReviewResponse(
            raw_text=text,
            usage=usage if isinstance(usage, dict) else None,
            model_reported=envelope["model"] if isinstance(envelope.get("model"), str) else None,
        )


def _answer_text(content: object) -> str:
    """The verdict text in an assistant message's `content`, or "" when there is none.

    The chat-completions shape admits a `content` that is a string or an array
    of typed parts, and a reasoning model that puts the answer beside its
    reasoning sends the array form. Accepting only the string form reported
    "returned no text content" for a submission that was billed and did carry
    a verdict, which is the one answer the caller cannot get back without
    paying for the same media twice. Only `text` parts are read: the array also
    carries non-text parts, and a `None` text is not a verdict.
    """
    if isinstance(content, str):
        return content if content.strip() else ""
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type", "text") == "text"
        and isinstance(part.get("text"), str)
    )


def build_body(request: ReviewRequest) -> dict[str, object]:
    """The chat-completions payload, as a plain dict (offline-testable).

    Local media travels as base64 data URLs: frames in `image_url` parts, a
    muxed video in a single `video_url` part (NVIDIA's documented form for
    video in chat completions), addressed from the text side by the same fixed
    attachment labels the prompt announces.

    The reviewer instruction goes in its own `system` message and the authored
    intent stays in the `user` message, so intent text cannot occupy or
    restate the instruction's slot.
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
    messages: list[dict[str, object]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append({"role": "user", "content": parts})
    return {
        "messages": messages,
        "model": request.model,
        **request.generation,
        "stream": False,
    }
