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

The walk is depth-bounded for the same reason `redact`'s is. `json.loads`
accepts nesting thousands of levels deep at the default recursion limit,
while a recursive Python walk over that tree runs out of stack and raises
`RecursionError` past its caller. On the provider boundary the caller is
`_http.post_json`, which maps that to a refusal; in `redact_json_text` the
caller is the evidence write for an already-billed submission. A container
past the limit is replaced by null, for the reason `redact` gives: a walk
that cannot finish cannot prove what the subtree holds.
"""

from __future__ import annotations

import math
from typing import Any

# How deep the walk descends before it stops. Real provider payloads (usage
# metadata, a model verdict) are three or four levels deep, so this is far
# above any honest structure. It matches `redaction.MAX_REDACT_DEPTH`, and the
# two must match: `redact_json_text` runs this over `redact`'s output, so a
# bound here that were lower than that one would truncate a document the
# redactor had already accepted whole.
MAX_WALK_DEPTH = 64


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


def strict_json_numbers(value: Any, _depth: int = 0) -> Any:
    """`value` with every non-finite float leaf replaced by None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        if _depth >= MAX_WALK_DEPTH:
            return None
        return {key: strict_json_numbers(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if _depth >= MAX_WALK_DEPTH:
            return None
        return [strict_json_numbers(item, _depth + 1) for item in value]
    return value
