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

from .. import config
from ..errors import DeadeyeError, no_verdict
from ..json_safe import finite_float
from ..prompt_text import flat_prompt_text
from ..sampling import IMAGE_SUFFIXES, VIDEO_SUFFIXES, MediaKind


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
    """The user turn: the authored intent, already fenced and declared data-only."""
    media: tuple[MediaPayload, ...]
    model: str
    timeout_seconds: float
    system_prompt: str = ""
    """The reviewer instruction: role, output contract, rubric, and the
    declaration that the user turn is data.

    Sent as the provider's system instruction, never concatenated into
    `prompt`. Keeping the two in separate roles is what stops authored text
    from occupying the slot the instruction lives in; an adapter whose
    endpoint has no system role sends `rendered` as a single turn instead."""

    @property
    def rendered(self) -> str:
        """Both halves as one string: the evidence text, and the single-turn form."""
        return f"{self.system_prompt}\n\n{self.prompt}" if self.system_prompt else self.prompt


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
        """Whether the credential material is present (environment or configuration).

        Presence only: this must never contact the provider, so capability
        discovery, `deadeye doctor`, and offline runs stay offline.
        """
        ...

    def configuration_hint(self) -> str:
        """How to configure it, naming the route and never any secret value."""
        ...

    def generation_settings(self) -> dict[str, Any]:
        """The generation parameters a submission to this provider sends.

        Sampling temperature, the output-token cap, and whatever else shapes
        the answer. Recorded in the evidence envelope under `provider.generation`,
        because two runs of the same clip, intent, and model differ in their
        verdicts for reasons the envelope has to name: without these, two
        envelopes recorded identically are two generations this tool cannot
        tell apart. An adapter with nothing to send (the fake) returns an
        empty object.
        """
        ...

    def review(self, request: ReviewRequest) -> ReviewResponse:
        """Submit media plus prompt; raise DeadeyeError on refusal or fault."""
        ...


class CredentialedProvider:
    """The credential, model, and media-limit half every hosted adapter shares.

    A hosted provider differs from its neighbours in the payload it builds and
    the envelope it reads back, not in where its key comes from or what it
    accepts: those three answers were the same shape in each adapter, and a
    copy of them is a copy that drifts. A subclass sets `name`,
    `credential_env_names`, `endpoint_mode`, `default_model_name`,
    `max_request_bytes`, and `max_frames_per_request`, implements
    `configuration_hint` and `review`, and inherits the rest.
    """

    name: str
    endpoint_mode: str
    credential_env_names: tuple[str, ...]
    default_model_name: str
    max_request_bytes: int
    max_frames_per_request: int

    requires_credential = True

    @property
    def default_model(self) -> str:
        return config.text(("providers", self.name, "model")) or self.default_model_name

    @property
    def limits(self) -> ProviderLimits:
        return ProviderLimits(
            suffixes=IMAGE_SUFFIXES + VIDEO_SUFFIXES,
            max_bytes=self.max_request_bytes,
            max_frames=self.max_frames_per_request,
            accepts_video=True,
            max_video_bytes=self.max_request_bytes,
        )

    def credential(self) -> str | None:
        """The configured key (environment first, then configuration), or None.

        Never logged; callers send it only.
        """
        return config.credential_for(self.name, self.credential_env_names)

    def is_configured(self) -> bool:
        return self.credential() is not None


def attachment_label(payload: MediaPayload) -> str:
    """How every adapter's prompt text addresses one attachment, by role.

    The filename is authored-local text, discovered from a clip directory or
    named by the author's own reference list, and it reaches the model inside
    the same user turn the author statement occupies. It is held to the full
    prompt-text rule: control characters are flattened so a name cannot forge
    extra label-shaped lines, and a name carrying an author-statement fence
    marker is refused rather than rendered, so it cannot close the data-only
    block the instruction declares. The refusal happens while the body is
    built, before any byte is sent.
    """
    name = flat_prompt_text(payload.name)
    if payload.kind == "video":
        return f"video attachment: {name}"
    if payload.kind == "reference":
        # A comparison asset may be a muxed video as well as a still (the
        # suffix table accepts both), and the label is the only place the
        # model is told which: calling a video an image misdescribes what the
        # model is looking at.
        noun = "video" if payload.mime_type.startswith("video/") else "image"
        return f"reference {noun}: {name}"
    return f"frame attachment: {name}"


def first_response_object(
    envelope: dict[str, Any],
    *,
    key: str,
    item_name: str,
    provider_name: str,
) -> dict[str, Any]:
    """The first object in a provider response list, or a refusal.

    Adapters use distinct envelope keys but share this contract: an absent or
    empty list means no verdict, while a non-list or non-object entry is a
    malformed provider response that must not escape as an AttributeError.

    Both are refusals on an answer that arrived, so both are spent
    submissions: a deduplicating caller records the key as spent rather than
    billing the same media a second time for an answer the provider has
    already shown it will not give.
    """
    entries = envelope.get(key)
    if not isinstance(entries, list) or not entries:
        raise no_verdict(f"provider {provider_name!r} returned no {item_name}")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise no_verdict(f"provider {provider_name!r} returned an invalid {item_name}")
    return entry


def response_object(
    entry: dict[str, Any],
    *,
    key: str,
    item_name: str,
    provider_name: str,
) -> dict[str, Any]:
    """The object at `entry[key]`, an empty one when absent, or a refusal.

    Both hosted envelopes nest the verdict one level down (`content` inside a
    candidate, `message` inside a choice). An absent level carries no verdict
    either, so it reads as an empty object and the caller's own emptiness
    check names the fault; a present-but-not-object level is a malformed
    provider response that must not escape as an AttributeError. Either way
    the response arrived, so the refusal is a spent submission: a `no_verdict`
    for the same reason.
    """
    value = entry.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise no_verdict(f"provider {provider_name!r} returned invalid {item_name} {key!r}")
    return value


def _unusable(provider: str, key: str, value: Any, expected: str) -> DeadeyeError:
    return DeadeyeError(
        f"config providers.{provider}.{key} must be {expected}, not {value!r}; "
        "fix it in config.toml or config.local.toml"
    )


def int_setting(provider: str, key: str, fallback: int, *, minimum: int = 0) -> int:
    """A provider's integer tuning knob (`providers.<name>.<key>`), or fallback.

    The one home every adapter reads its generation knobs through, so the
    type guard cannot drift between vendor modules. An absent key falls back
    to the built-in default; a value that is present but not an integer (a
    string, a list, a boolean — TOML spells booleans distinctly) is refused
    with the key named, before any submission. Silently substituting the
    default would send a request whose parameters differ from the ones the
    operator wrote down: exactly the misconfiguration a traceable review
    must not hide.

    `minimum` is the floor a present value must clear. A generation cap
    passes `minimum=1`: a provider that reads zero as "no limit", or treats a
    negative cap as absent, would turn a botched key into an unbounded
    billable generation, which is the one outcome these knobs exist to stop.
    """
    value = config.value(("providers", provider, key))
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unusable(provider, key, value, "an integer")
    if value < minimum:
        raise _unusable(provider, key, value, f"an integer of at least {minimum}")
    return value


def float_setting(
    provider: str,
    key: str,
    fallback: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """A provider's float tuning knob (`providers.<name>.<key>`), or fallback.

    Same contract as `int_setting`: absent falls back, present-but-unusable
    is refused with the key named. A non-finite value (`nan`, `inf`, `-inf`
    in TOML) is refused rather than passed through: it would reach the
    request body as a bare `NaN`/`Infinity` token that no JSON reader on the
    provider side accepts, and refusing beats sending a silently different
    parameter than the one configured. A TOML integer too large for a double
    is refused the same way, by name, rather than raising out of the
    narrowing that a refusal was supposed to replace.

    `minimum` and `maximum`, when given, are the inclusive range the
    provider's own API documents for that knob (`temperature` 0 to 2,
    `top_p` 0 to 1). A number is not refused for being a number alone: a
    `top_p` of 5 or a `temperature` of -1 is inside every type check and
    outside what the endpoint accepts, and the request is billed either way
    before the API answers with a validation error nobody reads. The
    refusal names the range, so the fix is a number rather than a search.
    """
    value = config.value(("providers", provider, key))
    if value is None:
        return fallback
    number = finite_float(value)
    if number is None:
        raise _unusable(provider, key, value, "a finite number")
    if minimum is not None and number < minimum:
        raise _unusable(provider, key, value, _range_text(minimum, maximum))
    if maximum is not None and number > maximum:
        raise _unusable(provider, key, value, _range_text(minimum, maximum))
    return number


def _range_text(minimum: float | None, maximum: float | None) -> str:
    """The expected-value text for a bounded knob, with either end optional."""
    if minimum is not None and maximum is not None:
        return f"a number from {minimum:g} to {maximum:g}"
    if minimum is not None:
        return f"a number of at least {minimum:g}"
    return f"a number of at most {maximum:g}"
