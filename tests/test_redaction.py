"""The redaction backstop every output path runs through."""

from __future__ import annotations

import json
import math
import sys
import unicodedata
from typing import Any

from deadeye.evidence import build_envelope
from deadeye.intent import ReviewIntent
from deadeye.redaction import (
    MAX_REDACT_DEPTH,
    SENSITIVE_KEY_PARTS,
    _is_sensitive_key,
    redact,
    redact_json_text,
)
from deadeye.sampling import SamplingRecord


def test_redact_drops_credential_keys_nested() -> None:
    value = {"ok": 1, "api_key": "secret", "headers": {"Authorization": "Bearer x", "meta": "y"}}
    assert redact(value) == {"ok": 1, "headers": {"meta": "y"}}


def test_redact_matches_case_fold_only_spellings() -> None:
    # The backstop folds case rather than lowering it: a key that differs from
    # a sensitive name only under case folding (the long s, U+017F, which
    # folds to ASCII 's') must not slip through as an ASCII-only blind spot.
    value = {"paſsword": "hunter2", "SECRET": "x", "keep": 1}  # noqa: RUF001
    assert redact(value) == {"keep": 1}


def test_redact_matches_keys_hiding_behind_invisible_format_characters() -> None:
    # Category Cf characters render as nothing, so `api<ZWSP>_key` holds no
    # `api_key` substring yet every reader, log, and re-serialization shows
    # `api_key`. A key that differs only by a Cf character names the same
    # thing, and a key that carries a visible glyph is a different key.
    value = {"api_key": "x", "pass\u200dword": "y", "keyz": 1, "keep": 2}
    assert redact(value) == {"keyz": 1, "keep": 2}
    assert _is_sensitive_key("api_key", SENSITIVE_KEY_PARTS) is True
    assert _is_sensitive_key("pass\u200dword", SENSITIVE_KEY_PARTS) is True
    # A key that reads differently once the invisible characters are gone is a
    # different key, so it is kept.
    assert _is_sensitive_key("keyz", SENSITIVE_KEY_PARTS) is False


def test_redact_stops_descending_at_the_depth_bound() -> None:
    # `json.loads` accepts nesting a recursive walk cannot survive, so the
    # bound is what keeps a hostile document from turning a billed submission
    # into a RecursionError. A container past the limit is replaced by null: a
    # walk that cannot finish cannot prove the subtree carries no credential.
    deep: dict[str, Any] = {"api_key": "secret"}
    for _ in range(MAX_REDACT_DEPTH + 4):
        deep = {"nested": deep}
    cleaned = redact(deep)
    levels = 0
    node = cleaned
    while isinstance(node, dict) and "nested" in node:
        node = node["nested"]
        levels += 1
    assert levels == MAX_REDACT_DEPTH
    assert node is None
    assert "secret" not in json.dumps(cleaned)


def test_the_sensitive_key_match_is_case_folded_not_normalized() -> None:
    # The key match is case folding plus invisible-character removal, and
    # deliberately no Unicode normalization. Pinned at the helper because the
    # choice is not obvious and a future pass must not quietly change it:
    #
    # - Every sensitive name is ASCII, so the NFD and NFC spellings of one are
    #   the same string already. NFC could only compose letters the match
    #   never looks at, leaving every outcome identical.
    # - Compatibility folding is a different question with a different answer:
    #   the fullwidth spelling renders as visibly wide letters, not as
    #   `api_key`, so it is a different key and stays.
    # - The backstop drops credential-named keys, it is not a normalizer, so
    #   two spellings of one benign key are both kept and neither is merged.
    #
    # What it does have to fold is case (U+017F -> 's') and invisible
    # formatting characters, which every reader renders as nothing at all.
    assert _is_sensitive_key("api_key", ("api_key",))
    assert _is_sensitive_key("API_KEY", ("api_key",))
    # Escaped so the source itself carries no lookalike characters.
    fullwidth = "\uff41\uff50\uff49\uff3f\uff4b\uff45\uff59"
    assert not _is_sensitive_key(fullwidth, ("api_key",))
    nfc = "caf\u00e9"
    nfd = "cafe\u0301"
    assert nfc != nfd and unicodedata.normalize("NFC", nfd) == nfc
    assert redact({nfc: 1, nfd: 2}) == {nfc: 1, nfd: 2}


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
            frames_available=0,
            frames_submitted=0,
            sampled=False,
            frame_indices=(),
            submitted_files=(),
            note="none",
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


def test_the_evidence_envelope_survives_a_deeply_nested_usage_payload() -> None:
    # The usage block and the request parameters reach `build_envelope`
    # straight from a provider, so the envelope is the one place the backstop
    # runs on structure it did not parse itself. A provider nesting its usage
    # metadata past the interpreter's stack limit used to raise
    # RecursionError out of the envelope of a submission that had already been
    # billed; the bound belongs on this path, not only on the raw-response one.
    from deadeye.intent import INTENT_SCHEMA_VERSION

    usage: dict[str, object] = {"api_key": "secret"}
    for _ in range(4000):
        usage = {"nested": usage}
    envelope = build_envelope(
        media_entries=(),
        sampling=SamplingRecord(
            frames_available=0,
            frames_submitted=0,
            sampled=False,
            frame_indices=(),
            submitted_files=(),
            note="no media",
        ),
        intent=ReviewIntent("p", "", "", "", (), (), (), "", ""),
        intent_raw=b"{}",
        provider_name="gemini",
        endpoint_mode="hosted-api:inline-base64",
        model_requested="m",
        model_reported=None,
        prompt="p",
        result=None,
        error=None,
        raw_response=None,
        usage=usage,
        total_bytes=0,
        params={},
        elapsed_seconds=0.0,
    )
    assert "secret" not in json.dumps(envelope)
    assert envelope["intent"]["schema_version"] == INTENT_SCHEMA_VERSION


def test_the_evidence_path_redacts_with_the_full_backstop() -> None:
    # The evidence envelope writes request parameters and usage through this
    # module, so every protection the backstop makes has to reach the stored
    # document: the hyphenated header names and keys hiding invisible
    # characters. A second copy of `redact` living beside the real one is how
    # those two went missing on this path.
    invisible = chr(0x200D)
    envelope = build_envelope(
        media_entries=(),
        sampling=SamplingRecord(0, 0, sampled=False, frame_indices=(), submitted_files=(), note=""),
        intent=ReviewIntent(
            purpose="p",
            subject="",
            camera_path="",
            desired_qualities="",
            avoid=(),
            references=(),
            questions=(),
            suite="",
            case="",
        ),
        intent_raw=b"",
        provider_name="gemini",
        endpoint_mode="default",
        model_requested="m",
        model_reported=None,
        prompt="",
        result=None,
        error=None,
        raw_response=None,
        usage=None,
        total_bytes=0,
        params={
            "x-goog-api-key": "AIza-header-shaped",
            f"sec{invisible}ret": "hidden-by-zero-width-join",
            "model": "m",
        },
        elapsed_seconds=0.0,
    )
    assert envelope["parameters"] == {"model": "m"}
    assert "AIza" not in json.dumps(envelope)
