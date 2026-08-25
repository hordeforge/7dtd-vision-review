"""The deadeye command line: `review`, `doctor`, `schema`, `prompt`, `mcp`.

The machine contract is the exit code and the JSON on stdout: `review --json`
prints the full evidence envelope, and every refusal exits non-zero with one
`ERROR: ...` line on stderr. Human-facing disclosure lines go to stderr so a
programmatic caller's stdout stays parseable. Usage misuse exits 2 (argparse),
an interrupt 130, a closed stdout pipe 141.

This module owns argument parsing and presentation only. The answers both
transports share (the provider registry, doctor states, the schema document,
the prompt preview) are built once in `surface.py`, which the MCP server also
imports; nothing here duplicates them.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from . import __version__, config
from .errors import DeadeyeError
from .review import run_review
from .surface import (
    PROVIDERS,
    _resolve_provider,
    _resolve_timeout,
    build_preview_prompt,
    provider_states,
    schema_document,
)


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
        description=(
            "submit a clip (frame directory or muxed video) plus intent to a "
            "vision model and print the review envelope. Every submission is "
            "one new billable upload to a third party and is never retried."
        ),
        epilog=(
            "examples:\n"
            "  deadeye doctor\n"
            "      which providers are usable right now; contacts nothing\n"
            "  deadeye prompt --intent intent.json --clip CLIP\n"
            "      render the exact reviewer prompt; submits nothing\n"
            "  deadeye review CLIP --intent intent.json \\\n"
            "      --provider fake --allow-network --json\n"
            "      the full envelope offline, for plumbing checks\n"
            "  deadeye review CLIP --intent intent.json \\\n"
            "      --provider gemini --allow-network --output evidence.json\n"
            "      a real, billable review, evidence kept beside the clip\n"
            "\n"
            "Exactly one of --intent PATH / --intent-text JSON is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    except KeyboardInterrupt:
        # Ctrl+C during a submission is not a fault to explain at length:
        # exit the way a SIGINT-killed process would (128 + SIGINT), with one
        # stderr line so a non-zero exit is never unexplained.
        print("ERROR: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # A downstream reader (`head`, a pager) closed the pipe on stdout.
        # Exit with the conventional SIGPIPE status (128 + SIGPIPE) instead
        # of failing a second time inside the interpreter's shutdown flush of
        # the buffered stream. Pointing stdout at devnull first is what keeps
        # that final flush quiet; it is deliberately best-effort, because an
        # embedded caller's stdout need not expose a file descriptor.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except (DeadeyeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # An unreadable clip, intent, or evidence path must meet the same
        # one-line refusal contract as every other failure, not a traceback:
        # the OS message already names the path and the reason.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


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


def _handle_prompt(args: argparse.Namespace) -> int:
    """Render the exact prompt the gateway would inject, without a review.

    This is the harness an agent (or a person) uses to see and verify what a
    review would ask the model, before anything is submitted.
    """
    print(build_preview_prompt(args.intent, args.intent_text, args.clip))
    return 0


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
        # Per-provider endpoint overrides, validated here so a bad one is
        # visible at diagnosis time instead of at review start. Pure config
        # validation: nothing is contacted.
        for name in sorted(PROVIDERS):
            problem = config.endpoint_problem(("providers", name, "endpoint"))
            if problem is not None:
                print(f"endpoint: {problem}")
    return 0


def _handle_schema(args: argparse.Namespace) -> int:
    print(json.dumps(schema_document(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
