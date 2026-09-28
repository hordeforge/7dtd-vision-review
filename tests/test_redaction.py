"""The redaction backstop every output path runs through."""

from __future__ import annotations

import json
import math
import sys
from typing import Any

from deadeye.redaction import MAX_REDACT_DEPTH, _is_sensitive_key, redact, redact_json_text


def test_redact_drops_credential_keys_nested() -> None:
    value = {"ok": 1, "api_key": "secret", "headers": {"Authorization": "Bearer x", "meta": "y"}}
    assert redact(value) == {"ok": 1, "headers": {"meta": "y"}}


def test_redact_drops_header_shaped_credential_keys() -> None:
    # A preserved raw response can echo the request it answered, and the
    # adapters send the key under a hyphenated header name. `api_key` alone
    # would not match `x-goog-api-key`.
    value = {"x-goog-api-key": "AIza-x", "x-api-key": "AIza-y", "keep": 1}
    assert redact(value) == {"keep": 1}


def test_redact_matches_case_fold_only_spellings() -> None:
    # The backstop folds case rather than lowering it: a key that differs from
    # a sensitive name only under case folding (the long s, U+017F, which
    # folds to ASCII 's') must not slip through as an ASCII-only blind spot.
    value = {"paſsword": "hunter2", "SECRET": "x", "keep": 1}  # noqa: RUF001
    assert redact(value) == {"keep": 1}


def test_redact_matches_keys_hiding_invisible_characters() -> None:
    # `api<ZWSP>_key` contains no `api_key` substring, yet it renders as
    # `api_key` in every log, viewer, and re-serialization, and the credential
    # is the same credential. Every format character (category Cf) is dropped
    # before the match so a key nobody can see cannot defeat the backstop.
    # The three keys below carry a ZWJ, a word joiner, and a left-to-right
    # embed, named by code point so the file's own text stays readable.
    zero_width_join = chr(0x200D)
    word_joiner = chr(0x2060)
    left_to_right_embed = chr(0x202A)
    value = {
        "api_key": "nvapi-x",
        f"sec{zero_width_join}ret": "y",
        f"pass{word_joiner}word": "z",
        f"k{left_to_right_embed}ey": "w",
        "keep": 1,
    }
    assert redact(value) == {"keep": 1}


def test_redact_keeps_a_key_that_differs_by_a_visible_glyph() -> None:
    # The policy strips what no reader can see and nothing else. A key spelled
    # with a different visible letter is a different key, not a hidden
    # spelling of a sensitive name: matching it is a homoglyph policy call,
    # and this backstop does not make one.
    assert not _is_sensitive_key("api_k" + chr(0xE9) + "y", ("api_key",))
    assert _is_sensitive_key("api_key", ("api_key",))


def test_redact_passes_nan_leaves_through_untouched() -> None:
    # Falsifying example from the fuzz suite: a NaN leaf compares unequal to
    # itself, so redaction must pass it through by identity for idempotence
    # to hold structurally at all.
    cleaned = redact({"ok": [float("nan")], "api_key": "secret"})
    assert list(cleaned) == ["ok"]
    assert len(cleaned["ok"]) == 1 and math.isnan(cleaned["ok"][0])


def test_redact_keeps_token_counters_for_usage() -> None:
    # The usage path redacts with USAGE_SENSITIVE_KEY_PARTS, which excludes
    # "token" (billing, not authentication), so a provider's totalTokenCount
    # survives while a credential-named key is still dropped.
    from deadeye.evidence import USAGE_SENSITIVE_KEY_PARTS

    value = {"totalTokenCount": 12, "secret": "x"}
    assert redact(value, USAGE_SENSITIVE_KEY_PARTS) == {"totalTokenCount": 12}


def test_redact_json_text_drops_credential_keys_from_a_document_string() -> None:
    # A raw provider response arrives as one string, which plain `redact()`
    # would pass through untouched however structured its contents are.
    cleaned = json.loads(
        redact_json_text(
            '{"summary": "verdict", "api_key": "nvapi-x", "meta": {"token": "t", "keep": 1}}'
        )
    )
    assert cleaned == {"summary": "verdict", "meta": {"keep": 1}}


def test_redact_drops_container_past_the_depth_limit() -> None:
    # `json.loads` accepts nesting far deeper than a recursive walk survives,
    # so the walk stops descending instead of raising RecursionError out of a
    # review that has already been billed. A subtree that cannot be examined
    # is dropped, not carried through unredacted.
    value: dict[str, object] = {"api_key": "secret"}
    for _ in range(MAX_REDACT_DEPTH + 5):
        value = {"nested": value}
    cleaned = redact(value)
    assert "secret" not in json.dumps(cleaned)
    # A shallow document is untouched by the bound.
    assert redact({"a": {"b": [1, 2]}}) == {"a": {"b": [1, 2]}}


def test_the_evidence_usage_walk_is_the_bounded_one() -> None:
    # The envelope redacts the provider's own usage block, and that walk is
    # the one that runs on the error path: a `RecursionError` out of it lands
    # after a billed submission. Two copies of this walk existed, one bounded
    # and one not, and the evidence path reached the unbounded one. Pin the
    # route `build_envelope` actually takes, not just the helper in isolation.
    # The depth is past the interpreter's own recursion limit, so the unbounded
    # walk raises here and only the bound survives.
    from deadeye.evidence import build_envelope
    from deadeye.intent import ReviewIntent
    from deadeye.sampling import SamplingRecord

    depth = sys.getrecursionlimit() + 100
    usage: dict[str, Any] = {"secret": "leaked", "totalTokenCount": 3}
    for _ in range(depth):
        usage = {"nested": usage}
    envelope = build_envelope(
        media_entries=(),
        sampling=SamplingRecord(
            0, 0, sampled=False, frame_indices=(), submitted_files=(), note="none"
        ),
        intent=ReviewIntent("p", "", "", "", (), (), (), "", ""),
        intent_raw=b"{}",
        provider_name="gemini",
        endpoint_mode="hosted",
        model_requested="m",
        model_reported=None,
        prompt="p",
        result=None,
        error=None,
        raw_response=None,
        usage=usage,
        total_bytes=0,
        params={"api_key": "leaked"},
        elapsed_seconds=0.0,
    )
    # Iterative descent: the document is deeper than any recursive walk here
    # can traverse, which is the whole point.
    leaves: list[object] = []
    frontier: list[object] = [envelope]
    while frontier:
        node = frontier.pop()
        if isinstance(node, dict):
            frontier.extend(node.values())
        elif isinstance(node, list):
            frontier.extend(node)
        else:
            leaves.append(node)
    assert "leaked" not in leaves


def test_redact_json_text_survives_a_deeply_nested_document() -> None:
    depth = 4000
    document = '{"a":' * depth + '{"api_key": "LEAK"}' + "}" * depth
    json.loads(document)  # the parser accepts it, so redaction must too
    cleaned = redact_json_text(document)
    assert "LEAK" not in cleaned


def test_redact_json_text_handles_an_array_document() -> None:
    cleaned = json.loads(redact_json_text('[{"api_key": "k"}, {"ok": 1}]'))
    assert cleaned == [{}, {"ok": 1}]


def test_redact_json_text_leaves_prose_scalars_and_broken_json_byte_identical() -> None:
    # Only structure-shaped text may be rewritten; anything else comes back
    # exactly as it arrived so the record stays honest about what was said.
    for text in (
        "the model declined to answer in JSON",
        'a bare scalar: "just words"',
        "42",
        "",
        "   ",
        '{"summary": "truncat',
        "{not json at all}",
    ):
        assert redact_json_text(text) == text


def test_redact_json_text_redacts_surrounding_whitespace_document() -> None:
    cleaned = json.loads(redact_json_text('  \n{"summary": "s", "secret": "v"}\n  '))
    assert cleaned == {"summary": "s"}


def test_redact_json_text_writes_no_bare_non_finite_token() -> None:
    # Re-serializing a document a provider answered with must not reintroduce
    # the bare `NaN`/`Infinity` tokens RFC 8259 does not define: the evidence
    # document carrying it would be unreadable by any strict parser.
    cleaned = redact_json_text(
        '{"summary": "s", "ratio": NaN, "burst": 1e999, "notes": [{"cost": -Infinity}]}'
    )
    assert "NaN" not in cleaned and "Infinity" not in cleaned
    assert json.loads(cleaned, parse_constant=_refuse_constant) == {
        "summary": "s",
        "ratio": None,
        "burst": None,
        "notes": [{"cost": None}],
    }


def _refuse_constant(name: str) -> object:
    raise AssertionError(f"non-finite token {name} reached the stored document")
