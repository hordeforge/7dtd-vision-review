"""The shared tool surface behind both transports: the CLI and the MCP server.

The gateway reaches its users two ways, and both must answer identically:
`deadeye doctor --json` and the MCP `doctor` tool return the same shapes by
contract, and so do `schema` and the rendered prompt preview. This module is
the single home each of those answers is built in, so they cannot drift into
two versions. Nothing here parses arguments or speaks a protocol; the
transports own presentation and framing.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, sampling
from .errors import DeadeyeError
from .intent import CAMERA_PATHS, INTENT_SCHEMA_VERSION, load_intent_file, parse_intent_text
from .prompt import build_prompt, preview_media
from .providers.fake import FakeProvider
from .providers.gemini import GeminiProvider
from .providers.nvidia import NvidiaProvider
from .result import BASE_RUBRIC, RESULT_KEYS, RUBRIC_VERSION
from .review import DEFAULT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .providers.base import VideoReviewProvider

# Provider registry: name -> constructor. `doctor` and `review --provider`
# both read this, so a new adapter is one line here plus its module.
PROVIDERS: dict[str, Callable[[], VideoReviewProvider]] = {
    "fake": FakeProvider,
    "gemini": GeminiProvider,
    "nvidia": NvidiaProvider,
}


def _resolve_provider(name: str | None) -> str:
    """The provider to use: the flag, else config's default_provider, else gemini.

    A configured but unknown name is refused, never silently swapped for the
    built-in default: a typo'd `default_provider` would otherwise send billable
    submissions to a different provider than the one configured.
    """
    if name:
        return name
    configured = config.value(("default_provider",))
    if configured is None or configured == "":
        return "gemini"
    if isinstance(configured, str) and configured in PROVIDERS:
        return configured
    raise DeadeyeError(
        f"config default_provider {configured!r} is not one of "
        f"{', '.join(sorted(PROVIDERS))}; fix it in config.toml or config.local.toml"
    )


def _resolve_timeout(raw: Any) -> float:
    """The seconds to wait for a provider: the flag, else config's
    timeout_seconds, else the built-in default.

    Validated here, before any submission, so an unusable value fails with one
    clear message instead of surfacing as an opaque error inside the HTTP
    stack. Zero and negative values are refused rather than silently read as
    "unset".
    """
    value = raw if raw is not None else config.value(("timeout_seconds",))
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeadeyeError(f"timeout must be a positive number of seconds, not {value!r}")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise DeadeyeError(f"timeout must be a positive number of seconds, not {value!r}")
    return seconds


def _credential_detail(provider: VideoReviewProvider) -> str:
    """Where the provider's credential came from, for doctor; never the value."""
    if not provider.requires_credential:
        # A keyless provider (the fake) must not be described as holding a
        # key just because some other provider's credential is configured.
        return provider.configuration_hint()
    from_env = any(os.environ.get(name) for name in provider.credential_env_names)
    from_local = config.value(("providers", provider.name, "api_key"))
    top_level = config.value(("api_key",))
    if from_env:
        return "key from environment"
    if isinstance(from_local, str) and from_local:
        return "key from config.local.toml"
    if isinstance(top_level, str) and top_level:
        return "key from config.local.toml (top-level api_key)"
    return provider.configuration_hint()


def build_preview_prompt(
    intent_path: Path | None, intent_text: str | None, clip: Path | None
) -> str:
    """The exact reviewer prompt for an intent, rendered without a review.

    The one home both surfaces share: `deadeye prompt` prints it and the MCP
    `prompt` tool wraps it, so they cannot drift. Exactly one intent route is
    required; `clip` is optional context for the media summary.
    """
    if intent_path is not None and intent_text is not None:
        raise DeadeyeError(
            "takes exactly one of --intent PATH / intent or --intent-text JSON, never both"
        )
    if intent_path is not None:
        intent, _ = load_intent_file(Path(intent_path))
    elif intent_text is not None:
        intent, _ = parse_intent_text(intent_text)
    else:
        raise DeadeyeError("needs exactly one of --intent PATH / intent or --intent-text JSON")

    media = sampling.discover(Path(clip)) if clip is not None else None
    media_summary, frame_note = preview_media(media)
    return build_prompt(
        intent, BASE_RUBRIC, media_summary=media_summary, frame_timing_note=frame_note
    )


def provider_states() -> list[dict[str, Any]]:
    """Per-provider capability state, for `doctor` on every surface.

    The single home both print: `deadeye doctor --json` and the MCP `doctor`
    tool return the same shapes by contract, so this is built once and never
    allowed to drift into two versions.
    """
    states: list[dict[str, Any]] = []
    for name, constructor in sorted(PROVIDERS.items()):
        provider = constructor()
        states.append(
            {
                "name": name,
                "endpoint_mode": provider.endpoint_mode,
                "state": "configured" if provider.is_configured() else "unavailable",
                "detail": _credential_detail(provider),
            }
        )
    return states


def schema_document() -> dict[str, Any]:
    """The intent and result schemas as one document.

    The single home both surfaces print: `deadeye schema` and the MCP
    `schema` tool return the same shapes by contract, so this is built once
    and never allowed to drift into two versions.
    """
    return {
        "intent": {
            "schema_version": INTENT_SCHEMA_VERSION,
            "required": ["purpose"],
            "fields": {
                "purpose": "string — what the clip is supposed to demonstrate",
                "subject": "string — the asset or behavior on screen",
                "camera_path": (f"string — one of {', '.join(CAMERA_PATHS)} or a free description"),
                "desired_qualities": (
                    "string — target proportions, silhouette, material read, timing"
                ),
                "avoid": "array of strings — clipping, popping, z-fighting, wrong scale, jitter",
                "references": "array of {path, purpose} comparison assets",
                "questions": "array of strings — concerns the reviewer must answer",
                "suite": "string — playtest suite id, for traceability",
                "case": "string — playtest case id, for traceability",
            },
        },
        "result": {
            "keys": list(RESULT_KEYS),
            "issues": "array of {description, at_seconds?: [start, end], at_frame?: [start, end]}",
            "rubric_scores": "0-5 or null per dimension",
            "confidence": "0-1",
            "rubric_version": RUBRIC_VERSION,
            "dimensions": [item.key for item in BASE_RUBRIC],
        },
    }
