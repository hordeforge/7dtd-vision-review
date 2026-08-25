"""The deadeye command line: `review`, `doctor`, `schema`.

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the full evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Human-facing disclosure lines go to stderr so a
programmatic caller's stdout stays parseable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .errors import DeadeyeError
from .providers.fake import FakeProvider
from .providers.gemini import GeminiProvider
from .review import run_review

if TYPE_CHECKING:
    from .providers.base import VideoReviewProvider

# Provider registry: name -> constructor. `doctor` and `review --provider`
# both read this, so a new adapter is one line here plus its module.
PROVIDERS: dict[str, Callable[[], VideoReviewProvider]] = {
    "fake": FakeProvider,
    "gemini": GeminiProvider,
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
        required=True,
        help="the vision-model provider to use",
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
        default=120.0,
        help="seconds to wait for the provider (default 120)",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except DeadeyeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _handle_review(args: argparse.Namespace) -> int:
    def notify(line: str) -> None:
        print(line, file=sys.stderr)

    envelope = run_review(
        args.clip,
        provider=PROVIDERS[args.provider](),
        intent_path=args.intent,
        intent_text=args.intent_text,
        model=args.model,
        allow_network=args.allow_network,
        timeout_seconds=args.timeout,
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
                "detail": provider.configuration_hint() if not configured else "credential present",
            }
        )
    if args.json:
        print(json.dumps(states, indent=2, sort_keys=True))
    else:
        for state in states:
            print(f"{state['name']}: {state['state']} ({state['detail']})")
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
