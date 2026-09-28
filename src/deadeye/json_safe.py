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

`loads` closes the same gap one level down, in the parser itself: CPython
refuses an integer literal longer than `sys.int_info.str_digits_check_threshold`
digits (4300 by default) with a bare `ValueError`, not a
`JSONDecodeError`, because the limit guards `int()` on a string rather than
JSON syntax. Every `except json.JSONDecodeError` in this tree therefore let
it through, and a document carrying one oversized literal escaped as an
unmapped `ValueError` at every parse boundary: a provider envelope, an
intent, a raw response, and a single MCP stdio frame that killed the
long-lived server. `loads` is the one door every one of those answers
through, and it reports the refusal as the `JSONDecodeError` the callers
already handle.
"""

from __future__ import annotations

import json
import math
from typing import Any

# How deep the walk descends before it stops. Real provider payloads (usage
# metadata, a model verdict) are three or four levels deep, so this is far
# above any honest structure. It matches `redaction.MAX_REDACT_DEPTH`, and the
# two must match: `redact_json_text` runs this over `redact`'s output, so a
# bound here that were lower than that one would truncate a document the
# redactor had already accepted whole.
MAX_WALK_DEPTH = 64


def loads(text: str) -> Any:
    """`json.loads`, with every parse refusal reported as `JSONDecodeError`.

    `json.JSONDecodeError` is a `ValueError`, but not every `ValueError` a
    `json.loads` raises is one of them. An integer literal past the
    interpreter's digit limit is refused by the `int()` the parser calls,
    and that raises the bare form with a message about digit limits rather
    than about JSON. Every parse boundary in this tree guards refusals with
    `JSONDecodeError`, so that case reached none of them.

    The consequence was a fault nobody had mapped: a provider envelope, an
    intent file, or a preserved raw response carrying one such literal left
    as a raw `ValueError` on a path whose contract is a refusal naming the
    input, and an MCP stdio frame carrying one took the whole long-lived
    server down instead of being answered with the spec's parse error.

    Only that one case is translated. `RecursionError` and every other
    exception the parser can raise keep their own type, because each is
    already handled on its own terms at the call sites: a caller that
    distinguishes "nested too deeply" from "not JSON" must still be able to.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise
    except ValueError as exc:
        # `JSONDecodeError(msg, doc, pos)`. The position is unknown: the
        # refusal came from converting one integer literal, not from a scan,
        # so any position would be a fiction. The message is the parser's
        # own, which names the limit that refused it.
        raise json.JSONDecodeError(str(exc), text, 0) from exc


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
    """`value` with every non-finite float leaf replaced by None.

    The walk is depth-bounded like `redact`'s: a container nested past
    `MAX_WALK_DEPTH` is replaced by None, because a walk that cannot finish
    cannot prove what the subtree holds.
    """
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
