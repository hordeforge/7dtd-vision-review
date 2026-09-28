"""The one redaction backstop every output path runs through.

Credentials are never accepted as arguments in the first place, so this is the
control behind that rule, not the rule itself: every document deadeye
hands a caller (the evidence envelope, stdout JSON, an MCP tool result) is
passed through `redact` first, so a credential-named key cannot land even
when a caller feeds the API a document it assembled itself.

It lives in its own module because its consumers are not one layer: the
review pipeline runs `redact_json_text` over a raw provider response, the
evidence envelope runs `redact` over request parameters and usage, and the
intent parser refuses fence markers. Colocating it with any of those would
have the other two import a parsing module to reach a security primitive.
Every consumer takes the backstop from here; a second copy in a parsing
module would be a control that answers to whichever path reached it. Two
copies of this walk existed once, one bounded and one not; the unbounded one
is what the evidence path called on the provider's own usage block, so a
usage document nested past the interpreter's recursion limit crashed a
submission that had already been billed. One home, one bound.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from .json_safe import strict_json_numbers

# Fields whose names look credential-bearing are dropped wherever they would
# otherwise land in stored evidence. Credentials are never accepted as
# arguments in the first place; this is the backstop for a caller that hands
# the API a document directly. `api-key` is the hyphenated spelling, so the
# header-shaped names the adapters actually send (`x-goog-api-key`,
# `x-api-key`) match the same way `api_key` does.
SENSITIVE_KEY_PARTS = (
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

# How deep `redact` walks a document before it stops descending. Real
# provider payloads (usage metadata, a model verdict) are three or four levels
# deep, so this is far above any honest structure and exists so the walk
# terminates on a hostile one.
MAX_REDACT_DEPTH = 64


def redact(
    value: Any,
    parts: tuple[str, ...] = SENSITIVE_KEY_PARTS,
    exceptions: tuple[str, ...] = (),
    _depth: int = 0,
) -> Any:
    """Deep-copy a JSON-shaped value, dropping credential-bearing mapping keys.

    The walk is depth-bounded at `MAX_REDACT_DEPTH`. `json.loads` accepts
    nesting far deeper than a recursive Python walk survives the stack, so an
    unbounded walk turned a deeply nested document into a `RecursionError`
    that escaped the refusal contract: on the CLI as a bare traceback, and on
    the evidence path (`redact(usage)`, `redact(params)`) as a crash of a
    submission that had already been billed. A container past the limit is
    replaced by null, because a walk that cannot finish cannot prove the
    subtree carries no credential.

    `exceptions` are folded key names that are never treated as sensitive,
    matched in full rather than as substrings. They exist for one caller: a
    provider's usage block reports its cost as `totalTokenCount` and friends,
    so the usage path cannot drop every token-shaped key, and dropping the
    whole `token` part instead is what lets `access_token`, `id_token`, and
    `refresh_token` through a backstop meant to catch them. An allowlist of
    the billing names keeps the counts and closes the rest.

    Whole-name matching is what keeps the allowlist from reopening the hole
    it closes: every billing name is a complete key (`totalTokenCount`,
    `prompt_tokens`), so a key that merely contains one (`access_token_total_tokens`)
    is still a credential-shaped key and is still dropped.
    """
    if isinstance(value, dict):
        if _depth >= MAX_REDACT_DEPTH:
            return None
        return {
            key: redact(item, parts, exceptions, _depth + 1)
            for key, item in value.items()
            if isinstance(key, str) and not _is_sensitive_key(key, parts, exceptions)
        }
    if isinstance(value, list):
        if _depth >= MAX_REDACT_DEPTH:
            return None
        return [redact(item, parts, exceptions, _depth + 1) for item in value]
    return value


def _is_sensitive_key(key: str, parts: tuple[str, ...], exceptions: tuple[str, ...] = ()) -> bool:
    # Case folding, not lower(): a key that differs from a sensitive name only
    # under case folding (long s U+017F folds to ASCII s) must not slip past
    # the backstop, and folding is locale-independent where this match must be.
    # Format characters go first: `api<ZWSP>_key` holds no `api_key`
    # substring, yet every reader, log, and re-serialization renders it as
    # `api_key`, so it names the same thing. Category Cf is the invisible set
    # (zero-width space and non-joiner, ZWJ, word joiner, the bidi controls),
    # joined by the variation selectors, which render as nothing but are
    # category Mn and so are matched by code point. A character with a visible
    # glyph is kept, because a key that reads differently is a different key.
    # No Unicode
    # normalization: every name in `parts` is ASCII, so NFC would compose
    # letters the match never looks at and leave the result identical.
    # No Cf code point is below U+0080 (the ASCII control range is Cc), so an
    # ASCII key cannot hide one and skips the per-character category walk,
    # which is the inner loop of every redacted document.
    folded = key.casefold() if key.isascii() else _strip_format_characters(key).casefold()
    # Whole-name, not substring: `parts` below is a substring test (a key
    # spelled `x-api-key` is a credential), but `exceptions` is an allowlist
    # and a substring test there is a hole rather than a convenience. Every
    # billing name is a complete key, so exact matching keeps them all.
    if folded in exceptions:
        return False
    return folded == "key" or any(part in folded for part in parts)


def _strip_format_characters(key: str) -> str:
    return "".join(
        char
        for char in key
        if unicodedata.category(char) != "Cf" and not _is_variation_selector(char)
    )


def _is_variation_selector(char: str) -> bool:
    # U+FE00..U+FE0F pick a glyph form for the character before them and render
    # as nothing on their own, so they hide a credential name exactly as a
    # zero-width space does. They are category Mn, not Cf, so the category walk
    # above cannot reach them; the tag characters (U+E0100..U+E01EF) that do
    # the same thing for emoji sequences are Cf and already covered.
    return 0xFE00 <= ord(char) <= 0xFE0F


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
