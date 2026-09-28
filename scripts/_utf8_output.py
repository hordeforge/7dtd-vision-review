"""Print non-ASCII text on a host whose locale is not UTF-8.

`scripts/e2e.sh` runs its helper scripts with a bare `python3`, so nothing
puts deadeye on the path and the library's own `_streams` binding never
applies. Every answer these scripts print is authored-local text: a provider
name and its credential detail from a config file, a Steam install path under
the account's home directory, and a verdict that is a model's own words. Under
C or POSIX (cron, a service unit, a CI job with no LANG) Python binds stdout to
ASCII and one such character raises out of `print`, which is how the e2e died
after a review was already billed and the verdict validated.

One binding for all three callers: three copies drifted into three separately
worded explanations, and the one place a fix has to land is the one place that
gets read. Same rationale as `deadeye._streams`, which the library binds for
itself; these scripts run with no deadeye import, so they carry their own.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO


def bind_utf8_output(stream: TextIO) -> None:
    """Bind `stream` to UTF-8 with backslash escapes, where that is possible.

    Best effort: a stream that is not a reconfigurable text wrapper (a pipe
    someone already rewound) keeps whatever encoding it has. `backslashreplace`
    on the streams that do bind is what covers the real crash, and a failure to
    bind must not stop a helper from answering.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    with contextlib.suppress(ValueError, OSError):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def bind_process_output() -> None:
    """Bind this process's stdout and stderr; the first thing a helper does."""
    bind_utf8_output(sys.stdout)
    bind_utf8_output(sys.stderr)
