"""Numbers that survive a strict reader, and the checks that admit them.

Python's JSON parser accepts the bare `NaN` and `Infinity` tokens RFC 8259
does not define, and an extreme exponent (`1e999`) silently parses to
infinity. Left in place, either one rides into evidence, stdout, and MCP
payloads that jq, a browser, or a Go consumer cannot parse back. A non-finite
number becomes null; every finite value and every key passes through
untouched. The provider boundary and the redacting serializer both hold the
same rule, so it lives here once.

The mirror of that rule is `finite_float`: a parser hands a bare integer
literal to Python as an `int` of any size (`tomllib` included), and
`math.isfinite` and `float()` both raise `OverflowError` on one too large to
represent rather than answering. Every number arriving from a parse boundary
is admitted through it, so an unparseable size is refused with the message
that names the field instead of unwinding the refusal path on a traceback.
"""

from __future__ import annotations

import math
from typing import Any


def finite_float(value: Any) -> float | None:
    """`value` as a finite float, or None when it is not one.

    A bool is not a number here: `True` reads as 1 in arithmetic and as a
    string-shaped answer in a verdict, and a configuration knob set to it is a
    typo, not a temperature of 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # An integer literal too large for a double. It is not infinity
        # (no float can hold it) and not a usable measurement; the caller's
        # refusal names it, which is what a four-hundred-digit `at_frame`
        # deserves instead of a crash on a billed submission.
        return None
    return number if math.isfinite(number) else None


def strict_json_numbers(value: Any) -> Any:
    """`value` with every non-finite float leaf replaced by None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: strict_json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strict_json_numbers(item) for item in value]
    return value
