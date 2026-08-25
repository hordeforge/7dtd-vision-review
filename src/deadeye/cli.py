"""The deadeye command line: `review`, `doctor`, `schema`, `prompt`, `mcp`.

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the full evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Human-facing disclosure lines go to stderr so a
programmatic caller's stdout stays parseable.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, config
from .errors import DeadeyeError
from .providers.fake import FakeProvider
from .providers.gemini import GeminiProvider
from .providers.nvidia import NvidiaProvider
from .review import DEFAULT_TIMEOUT_SECONDS, run_review

if TYPE_CHECKING:
    from .providers.base import VideoReviewProvider

# Provider registry: name -> constructor. `doctor` and `review --provider`
# both read this, so a new adapter is one line here plus its module.
PROVIDERS: dict[str, Callable[[], VideoReviewProvider]] = {
    "fake": FakeProvider,
    "gemini": GeminiProvider,
    "nvidia": NvidiaProvider,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadeye",
        description=(
            "vision-model review gateway: forward frames or a muxed clip plus "
            "recorded intent to a vision model and get structured, advisory "
            "feedback. Submitting media is networked, billable, and sends "
            "authored assets to a third party: it never happens without "
            "--allow-network."
        ),
    )
    parser.add_argument("--version", action="version", version=f"deadeye {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser(
        "review",
        help="submit a clip (frame directory or muxed video) plus intent to a "
        "vision model and print the review envelope",
    )
    review.add_argument("clip", type=Path, help="a clip directory or muxed video file")
    review.add_argument(
        "--intent",
        type=Path,
        help="the intent JSON file committed beside the source (the reproducible route)",
    )
    review.add_argument("--intent-text", help="inline intent JSON instead of --intent")
    review.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        help="the vision-model provider to use (default: config default_provider)",
    )
    review.add_argument("--model", help="provider model identifier; default per provider")
    review.add_argument(
        "--allow-network",
        action="store_true",
        help="consent to uploading the media to the provider (required)",
    )
    review.add_argument("--json", action="store_true", help="print the full evidence envelope")
    review.add_argument("--output", type=Path, help="write the evidence envelope to PATH")
    review.add_argument(
        "--keep-raw-response",
        action="store_true",
        help="preserve a redacted copy of the provider's raw response in evidence",
    )
    review.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait for the provider (default: config timeout_seconds or 120)",
    )
    review.add_argument(
        "--force",
        action="store_true",
        help="overwrite an earlier evidence envelope at --output",
    )
    review.set_defaults(handler=_handle_review)

    doctor = subparsers.add_parser(
        "doctor",
        help="report provider capability state without contacting any provider",
    )
    doctor.add_argument("--json", action="store_true", help="print an array of provider states")
    doctor.set_defaults(handler=_handle_doctor)

    schema = subparsers.add_parser(
        "schema",
        help="print the intent and result schemas as JSON",
    )
    schema.set_defaults(handler=_handle_schema)

    prompt = subparsers.add_parser(
        "prompt",
        help="render the exact reviewer prompt the gateway injects for an "
        "intent, without running a review",
    )
    prompt.add_argument(
        "--intent",
        type=Path,
        help="the intent JSON file (the reproducible route)",
    )
    prompt.add_argument("--intent-text", help="inline intent JSON instead of --intent")
    prompt.add_argument(
        "--clip",
        type=Path,
        help="derive the media summary from a real clip (optional; otherwise a "
        "generic summary is used)",
    )
    prompt.set_defaults(handler=_handle_prompt)

    mcp = subparsers.add_parser(
        "mcp",
        help="serve the same surface as a Model Context Protocol server on stdio",
    )
    mcp.set_defaults(handler=_handle_mcp)
    return parser


def _handle_mcp(args: argparse.Namespace) -> int:
    from .mcp import serve

    return serve()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (DeadeyeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # An unreadable clip, intent, or evidence path must meet the same
        # one-line refusal contract as every other failure, not a traceback:
        # the OS message already names the path and the reason.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


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


def _handle_review(args: argparse.Namespace) -> int:
    def notify(line: str) -> None:
        print(line, file=sys.stderr)

    provider_name = _resolve_provider(args.provider)
    timeout = _resolve_timeout(args.timeout)
    envelope = run_review(
        args.clip,
        provider=PROVIDERS[provider_name](),
        intent_path=args.intent,
        intent_text=args.intent_text,
        model=args.model,
        allow_network=args.allow_network,
        timeout_seconds=timeout,
        keep_raw_response=args.keep_raw_response,
        output=args.output,
        force=args.force,
        notify=notify,
    )
    if args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        result = envelope["result"]
        print(f"provider: {envelope['provider']['name']}")
        reported = envelope["provider"]["model_reported"]
        requested = envelope["provider"]["model_requested"]
        print(f"model: {reported or requested}")
        print(f"summary: {result['summary']}")
        print(f"issues: {len(result['issues'])}")
        if envelope["evidence"]["path"]:
            print(f"evidence: {envelope['evidence']['path']}")
    return 0


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
    from . import sampling
    from .intent import load_intent_file, parse_intent_text
    from .prompt import build_prompt, preview_media
    from .result import BASE_RUBRIC

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


def _handle_prompt(args: argparse.Namespace) -> int:
    """Render the exact prompt the gateway would inject, without a review.

    This is the harness an agent (or a person) uses to see and verify what a
    review would ask the model, before anything is submitted.
    """
    print(build_preview_prompt(args.intent, args.intent_text, args.clip))
    return 0


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


def _handle_doctor(args: argparse.Namespace) -> int:
    states = provider_states()
    try:
        sources = config.load().sources()
    except ValueError:
        # A broken config is reported below, never a crash.
        sources = []
    load_failure = config.load_failure()
    if args.json:
        print(json.dumps(states, indent=2, sort_keys=True))
    else:
        for state in states:
            print(f"{state['name']}: {state['state']} ({state['detail']})")
        if sources:
            print("config: " + ", ".join(str(path) for path in sources))
        else:
            print("config: none (see config.toml and config.local.toml.example)")
        if load_failure:
            print(f"config error: {load_failure}")
        note = config.discovery_note()
        if note:
            print(f"config note: {note}")
        # The effective top-level knobs, so a misconfiguration is visible
        # without reading the files; never any credential material here.
        try:
            print(f"default_provider: {_resolve_provider(None)}")
        except DeadeyeError as exc:
            print(f"default_provider: not usable ({exc})")
        default_model = config.value(("default_model",))
        if isinstance(default_model, str) and default_model:
            print(f"default_model: {default_model}")
        try:
            print(f"timeout_seconds: {_resolve_timeout(None):g}")
        except DeadeyeError as exc:
            print(f"timeout_seconds: not usable ({exc})")
    return 0


def schema_document() -> dict[str, Any]:
    """The intent and result schemas as one document.

    The single home both surfaces print: `deadeye schema` and the MCP
    `schema` tool return the same shapes by contract, so this is built once
    and never allowed to drift into two versions.
    """
    from .intent import CAMERA_PATHS, INTENT_SCHEMA_VERSION
    from .result import BASE_RUBRIC, RESULT_KEYS, RUBRIC_VERSION

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


def _handle_schema(args: argparse.Namespace) -> int:
    print(json.dumps(schema_document(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
