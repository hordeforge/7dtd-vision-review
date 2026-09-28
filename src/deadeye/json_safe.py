"""JSON values that survive a strict reader.

Python's JSON parser accepts the bare `NaN` and `Infinity` tokens RFC 8259
does not define, and an extreme exponent (`1e999`) silently parses to
infinity. Left in place, either one rides into evidence, stdout, and MCP
payloads that jq, a browser, or a Go consumer cannot parse back. A non-finite
number becomes null; every finite value and every key passes through
untouched. The provider boundary and the redacting serializer both hold the
same rule, so it lives here once.
"""

from __future__ import annotations

import math
from typing import Any


def strict_json_numbers(value: Any) -> Any:
    """`value` with every non-finite float leaf replaced by None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: strict_json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strict_json_numbers(item) for item in value]
    return value
