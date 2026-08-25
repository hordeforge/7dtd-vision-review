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

from dataclasses import dataclass
from typing import Any, Protocol


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
    kind: str
    """'frame', 'video', or 'reference' — how the prompt addresses it."""
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

    @property
    def default_model(self) -> str: ...

    @property
    def limits(self) -> ProviderLimits: ...

    def is_configured(self) -> bool:
        """Whether the credential material is present in the environment.

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
