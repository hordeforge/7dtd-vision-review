"""The one redaction backstop every output path runs through.

Credentials are never accepted as arguments in the first place, so this is
the control behind that rule, not the rule itself: every document deadeye
hands a caller (the evidence envelope, stdout JSON, an MCP tool result) is
passed through `redact` first, so a credential-named key cannot land even
when a caller feeds the API a document it assembled itself.

It lives in its own module because its consumers are not one layer: the
review pipeline runs `redact_json_text` over a raw provider response, the
evidence envelope runs `redact` over request parameters and usage, and the
intent parser refuses fence markers. Colocating it with any of those would
have the other two import a parsing module to reach a security primitive.
"""

from __future__ import annotations

import json
from typing import Any

from .json_safe import strict_json_numbers

# Fields whose names look credential-bearing are dropped wherever they would
# otherwise land in stored evidence.
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def redact(value: Any, parts: tuple[str, ...] = SENSITIVE_KEY_PARTS) -> Any:
    """Deep-copy a JSON-shaped value, dropping credential-bearing mapping keys."""
    if isinstance(value, dict):
        return {
            key: redact(item, parts)
            for key, item in value.items()
            if isinstance(key, str) and not _is_sensitive_key(key, parts)
        }
    if isinstance(value, list):
        return [redact(item, parts) for item in value]
    return value


def _is_sensitive_key(key: str, parts: tuple[str, ...]) -> bool:
    # Case folding, not lower(): a key that differs from a sensitive name only
    # under case folding (long s U+017F folds to ASCII s) must not slip past
    # the backstop, and folding is locale-independent where this match must be.
    folded = key.casefold()
    return folded == "key" or any(part in folded for part in parts)


def redact_json_text(text: str, parts: tuple[str, ...] = SENSITIVE_KEY_PARTS) -> str:
    """Redact credential-bearing keys from a JSON-encoded document string.

    A raw provider response arrives as one string, which plain `redact()`
    would return untouched however structured its contents are: the backstop
    walks mappings, and a string is a leaf. When the text parses as a JSON
    object or array, its mapping keys are redacted and the document
    re-serialized; anything else (model prose, a bare scalar, broken or
    truncated JSON) comes back byte-identical: there is nothing
    structure-shaped to clean, and guessing further would rewrite the record.

    A non-finite number (`NaN`, `1e999`) is neutralized on the way out:
    re-serializing it would write the bare `NaN`/`Infinity` token that RFC
    8259 does not define, so the stored evidence document could no longer be
    read back by any strict parser.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    return json.dumps(strict_json_numbers(redact(parsed, parts)))
