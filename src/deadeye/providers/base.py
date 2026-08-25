"""The provider boundary for vision-model review.

An adapter is deliberately narrow: it knows its credential environment, the
media formats, frame count, and payload size it accepts, how to submit frames
or a video plus a prompt, and how to bring back raw text plus usage metadata.
Everything else — intent validation, rubric, result schema, sampling, evidence
— belongs to the deadeye core and is identical across providers, so adding one
never forks the contract.

Adapters speak HTTP with the standard library. A build tool that already
carries no SDK has no reason to grow one, and every dependency avoided here is
a supply-chain surface a consuming mod author never has to audit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from .. import config
from ..errors import DeadeyeError
from ..sampling import MediaKind, flat_label_text


@dataclass(frozen=True)
class ProviderLimits:
    """What this provider accepts, so refusal happens locally and cheaply."""

    suffixes: tuple[str, ...]
    """Filename suffixes (lowercase, dot included) the endpoint consumes."""
    max_bytes: int | None
    """Total media bytes per request; None when the provider publishes no bound."""
    max_frames: int | None
    """Maximum images per request when submitting a frame sequence; None = no cap."""
    accepts_video: bool
    """Whether a muxed video file can be submitted as-is."""
    max_video_bytes: int | None
    """Per-video byte budget when `accepts_video`; None when unpublished."""


@dataclass(frozen=True)
class MediaPayload:
    """One submitted file's exact bytes, name, content type, and role."""

    name: str
    mime_type: str
    kind: MediaKind
    """How the prompt addresses it: 'frame', 'video', or 'reference'."""
    data: bytes


@dataclass(frozen=True)
class ReviewRequest:
    """Everything a submission needs, assembled by the deadeye core."""

    prompt: str
    media: tuple[MediaPayload, ...]
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class ReviewResponse:
    """The boundary's output: raw text, verbatim usage, what the model said."""

    raw_text: str
    usage: dict[str, Any] | None
    """Provider-reported token counts, passed through untouched or None."""
    model_reported: str | None
    """The model identifier as the provider states it, when it does."""


class VideoReviewProvider(Protocol):
    """One hosted vision-capable model endpoint."""

    name: str
    endpoint_mode: str
    requires_credential: bool
    credential_env_names: tuple[str, ...]
    """Environment variables a credential may arrive in; empty when keyless."""

    @property
    def default_model(self) -> str: ...

    @property
    def limits(self) -> ProviderLimits: ...

    def is_configured(self) -> bool:
        """Whether the credential material is present (environment or local config).

        Presence only: this must never contact the provider, so capability
        discovery, `deadeye doctor`, and offline runs stay offline.
        """
        ...

    def configuration_hint(self) -> str:
        """How to configure it, naming the route and never any secret value."""
        ...

    def review(self, request: ReviewRequest) -> ReviewResponse:
        """Submit media plus prompt; raise DeadeyeError on refusal or fault."""
        ...


def attachment_label(payload: MediaPayload) -> str:
    """How every adapter's prompt text addresses one attachment, by role.

    The filename is authored-local text interpolated outside the author
    statement's data-only fence, so control characters are flattened: a name
    carrying a newline cannot forge extra label-shaped lines beside it.
    """
    name = flat_label_text(payload.name)
    if payload.kind == "video":
        return f"video attachment: {name}"
    if payload.kind == "reference":
        return f"reference image: {name}"
    return f"frame attachment: {name}"


def _unusable(provider: str, key: str, value: Any, expected: str) -> DeadeyeError:
    return DeadeyeError(
        f"config providers.{provider}.{key} must be {expected}, not {value!r}; "
        "fix it in config.toml or config.local.toml"
    )


def int_setting(provider: str, key: str, fallback: int) -> int:
    """A provider's integer tuning knob (`providers.<name>.<key>`), or fallback.

    The one home every adapter reads its generation knobs through, so the
    type guard cannot drift between vendor modules. An absent key falls back
    to the built-in default; a value that is present but not an integer (a
    string, a list, a boolean — TOML spells booleans distinctly) is refused
    with the key named, before any submission. Silently substituting the
    default would send a request whose parameters differ from the ones the
    operator wrote down: exactly the misconfiguration a traceable review
    must not hide.
    """
    value = config.value(("providers", provider, key))
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unusable(provider, key, value, "an integer")
    return value


def float_setting(provider: str, key: str, fallback: float) -> float:
    """A provider's float tuning knob (`providers.<name>.<key>`), or fallback.

    Same contract as `int_setting`: absent falls back, present-but-unusable
    is refused with the key named. A non-finite value (`nan`, `inf`, `-inf`
    in TOML) is refused rather than passed through: it would reach the
    request body as a bare `NaN`/`Infinity` token that no JSON reader on the
    provider side accepts, and refusing beats sending a silently different
    parameter than the one configured.
    """
    value = config.value(("providers", provider, key))
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _unusable(provider, key, value, "a finite number")
    if not math.isfinite(float(value)):
        raise _unusable(provider, key, value, "a finite number")
    return float(value)
