"""The deadeye command line: `review`, `doctor`, `schema`.

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the full evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Human-facing disclosure lines go to stderr so a
programmatic caller's stdout stays parseable.
"""

from __future__ import annotations

import argparse
import json
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
from .review import run_review

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (DeadeyeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _resolve_provider(name: str | None) -> str:
    """The provider to use: the flag, else config's default_provider, else gemini."""
    if name:
        return name
    configured = config.value(("default_provider",))
    if isinstance(configured, str) and configured in PROVIDERS:
        return configured
    return "gemini"


def _handle_review(args: argparse.Namespace) -> int:
    def notify(line: str) -> None:
        print(line, file=sys.stderr)

    provider_name = _resolve_provider(args.provider)
    timeout = args.timeout or config.value(("timeout_seconds",)) or 120.0
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
    env_names = provider.credential_env_names if hasattr(provider, "credential_env_names") else ()
    from_env = any(os.environ.get(name) for name in env_names)
    from_local = config.value(("providers", provider.name, "api_key"))
    top_level = config.value(("api_key",))
    if from_env:
        return "key from environment"
    if isinstance(from_local, str) and from_local:
        return "key from config.local.toml"
    if isinstance(top_level, str) and top_level:
        return "key from config.local.toml (top-level api_key)"
    return provider.configuration_hint()


def _handle_prompt(args: argparse.Namespace) -> int:
    """Render the exact prompt the gateway would inject, without a review.

    This is the harness an agent (or a person) uses to see and verify what a
    review would ask the model, before anything is submitted.
    """
    from . import sampling
    from .intent import load_intent_file, parse_intent_text
    from .prompt import build_prompt
    from .result import BASE_RUBRIC

    if args.intent is not None and args.intent_text is not None:
        raise DeadeyeError(
            "deadeye prompt takes exactly one of --intent PATH or --intent-text JSON, never both"
        )
    if args.intent is not None:
        intent, _ = load_intent_file(Path(args.intent))
    elif args.intent_text is not None:
        intent, _ = parse_intent_text(args.intent_text)
    else:
        raise DeadeyeError(
            "deadeye prompt needs exactly one of --intent PATH or --intent-text JSON"
        )

    if args.clip is not None:
        media = sampling.discover(Path(args.clip))
        if media.video is not None:
            media_summary = f"a single muxed video file ({media.video.name})"
            frame_note = ""
        else:
            media_summary = (
                f"{len(media.frames)} frame image(s) of the clip's {len(media.frames)} frames"
            )
            frame_note = (
                "Frames arrive in the order listed; an issue's at_frame index refers to "
                "that order, while at_seconds refers to seconds from the clip's start."
            )
    else:
        media_summary = "the submitted media (a muxed video or a sampled frame sequence)"
        frame_note = ""
    print(
        build_prompt(intent, BASE_RUBRIC, media_summary=media_summary, frame_timing_note=frame_note)
    )
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    states: list[dict[str, Any]] = []
    for name, constructor in sorted(PROVIDERS.items()):
        provider = constructor()
        configured = provider.is_configured()
        states.append(
            {
                "name": name,
                "endpoint_mode": provider.endpoint_mode,
                "state": "configured" if configured else "unavailable",
                "detail": _credential_detail(provider),
            }
        )
    load_failure = config.load_failure()
    try:
        sources = config.load().sources()
    except ValueError:
        # A broken config is reported below, never a crash.
        sources = []
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
    return 0


def _handle_schema(args: argparse.Namespace) -> int:
    from .intent import CAMERA_PATHS, INTENT_SCHEMA_VERSION
    from .result import BASE_RUBRIC, RESULT_KEYS, RUBRIC_VERSION

    schema = {
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
    print(json.dumps(schema, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
