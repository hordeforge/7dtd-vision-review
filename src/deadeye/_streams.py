"""Output streams that cannot lose a verdict to an unencodable character.

A review's prose comes from a model and its labels come from filenames, so
neither is ASCII by construction. Python binds stdout to the locale's
encoding, and under C or POSIX (cron, a systemd unit, a CI job with no LANG)
that encoding is ASCII: one non-ASCII character then raises
`UnicodeEncodeError` inside `print`, after the submission was already billed
and the verdict validated, so the caller loses exactly the result it paid for.
Both presentation streams are therefore bound to UTF-8 with
`backslashreplace`, which renders an unrepresentable character as an escape
instead of raising.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO

__all__ = ["bind_process_output", "bind_utf8_output"]


def bind_utf8_output(stream: TextIO) -> None:
    """Bind `stream` to UTF-8 with backslash escapes, where that is possible.

    Best effort: a stream that is not a reconfigurable text wrapper (a
    `StringIO` in a test, a pipe someone already rewound) keeps whatever
    encoding it has. That is the caller's choice of stream, and a failure to
    bind must not stop the tool from starting over it; `backslashreplace` on
    the streams that do bind is what covers the real crash.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    with contextlib.suppress(ValueError, OSError):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def bind_process_output() -> None:
    """Bind this process's stdout and stderr; the first thing a run does."""
    bind_utf8_output(sys.stdout)
    bind_utf8_output(sys.stderr)
