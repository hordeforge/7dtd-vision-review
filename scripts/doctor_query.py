"""Answer one question about `deadeye doctor --json`, reading it on stdin.

The e2e needs three reads of the same array (which provider to use, whether
that one is configured, why it is not); each was a `python3 -c` body inside
`e2e.sh`, where the JSON parsing was invisible to the linter and the type
checker. One named entry point per question keeps the shell to argument
passing.

Usage:
  doctor_query.py select [PREFERRED]   the provider to review with, or nothing
  doctor_query.py state NAME           that provider's state, or nothing
  doctor_query.py detail NAME          that provider's credential detail

Every command prints one line (nothing when the answer is "none") and exits
0, so the caller tests the output rather than the status.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from _utf8_output import bind_process_output

USAGE = "usage: doctor_query.py {select [NAME]|state NAME|detail NAME} < doctor.json"
FAKE_PROVIDER = "fake"

# How many arguments each command takes, as `USAGE` publishes it. The one
# place a command is recognized, so a name that is not here and a call that
# gives the wrong number of arguments are refused the same way.
_ARGUMENT_COUNTS: dict[str, tuple[int, ...]] = {
    "select": (0, 1),
    "state": (1,),
    "detail": (1,),
}


def _read_doctor_output() -> str:
    """The doctor's stdout as text, decoded the way it was written.

    `deadeye doctor --json` binds its own stdout to UTF-8, so these bytes are
    UTF-8 whatever the reading process's locale is. Decoding them through
    `sys.stdin` hands the choice to that locale instead; under C or POSIX
    that is ASCII, and the decode the interpreter installs there turns an
    invalid byte into a lone surrogate that resurfaces as a mangled provider
    name rather than as the fault it is. UTF-8, strictly, refuses it here.
    """
    source = getattr(sys.stdin, "buffer", None)
    raw = source.read() if source is not None else sys.stdin.read()
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _states(raw: str) -> list[dict[str, Any]]:
    parsed: Any = json.loads(raw)
    if not isinstance(parsed, list):
        raise SystemExit(f"{USAGE}\n\ndoctor output is not an array: {type(parsed).__name__}")
    return [entry for entry in parsed if isinstance(entry, dict)]


def _state(states: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((entry for entry in states if entry.get("name") == name), None)


def _select(states: list[dict[str, Any]], preferred: str) -> str:
    """The configured provider to use: the preferred one when it is
    configured, else the first configured real provider, else nothing."""
    chosen = _state(states, preferred) if preferred else None
    if chosen is not None and chosen.get("state") == "configured":
        return preferred
    for entry in states:
        if entry.get("state") == "configured" and entry.get("name") != FAKE_PROVIDER:
            return str(entry.get("name", ""))
    return ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    command, arguments = argv[1], argv[2:]
    # The command and its argument count settle together, before stdin is read:
    # every command publishes one accepted count in `USAGE`, and a caller who
    # got it wrong is answered with the usage line rather than by a question
    # about the doctor's output it did not mean to ask.
    if command not in _ARGUMENT_COUNTS or len(arguments) not in _ARGUMENT_COUNTS[command]:
        print(USAGE, file=sys.stderr)
        return 2
    bind_process_output()
    try:
        states = _states(_read_doctor_output())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"doctor_query.py: doctor output is not JSON: {exc}", file=sys.stderr)
        return 1

    if command == "select":
        print(_select(states, arguments[0] if arguments else ""))
        return 0

    entry = _state(states, arguments[0])
    if entry is None:
        return 0
    if command == "state":
        print(entry.get("state", ""))
    else:
        print(entry.get("detail", "no credential configured"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
