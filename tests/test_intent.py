"""Intent document validation."""

from __future__ import annotations

import json
import math

import pytest

from deadeye.errors import DeadeyeError
from deadeye.intent import (
    CAMERA_PATHS,
    load_intent_file,
    parse_intent,
    parse_intent_text,
    redact,
)


def test_a_valid_intent_parses(intent_bytes: bytes) -> None:
    intent = parse_intent(json.loads(intent_bytes), "intent")
    assert intent.purpose == "show the garment survives a full turn without clipping"
    assert intent.camera_path == "turntable"
    assert intent.subject == ""
    assert intent.avoid == ()


def test_purpose_is_required_and_non_empty() -> None:
    with pytest.raises(DeadeyeError, match="missing required field 'purpose'"):
        parse_intent({"camera_path": "turntable"}, "intent")
    with pytest.raises(DeadeyeError, match="'purpose' must not be empty"):
        parse_intent({"purpose": "   "}, "intent")


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(DeadeyeError, match="unknown intent field"):
        parse_intent({"purpose": "x", "intended_use": "y"}, "intent")


def test_unsupported_schema_version_is_refused() -> None:
    with pytest.raises(DeadeyeError, match="schema_version"):
        parse_intent({"schema_version": 99, "purpose": "x"}, "intent")


def test_references_need_path_and_purpose() -> None:
    intent = parse_intent(
        {
            "purpose": "x",
            "references": [{"path": "refs/good.png", "purpose": "known-good silhouette"}],
        },
        "intent",
    )
    assert intent.references[0].path.name == "good.png"
    with pytest.raises(DeadeyeError, match="exactly 'path' and 'purpose'"):
        parse_intent({"purpose": "x", "references": [{"path": "a.png"}]}, "intent")
    with pytest.raises(DeadeyeError, match="'path' must be a non-empty string"):
        parse_intent({"purpose": "x", "references": [{"path": "", "purpose": "why"}]}, "intent")


def test_camera_paths_are_documented() -> None:
    assert "turntable" in CAMERA_PATHS
    assert "walk-cycle" in CAMERA_PATHS


def test_intent_text_round_trip_carries_exact_bytes(intent_bytes: bytes) -> None:
    intent, raw = parse_intent_text(intent_bytes.decode("utf-8"))
    assert raw == intent_bytes
    assert intent.purpose


def test_intent_file_round_trip_carries_exact_bytes(tmp_path, intent_path) -> None:
    intent, raw = load_intent_file(intent_path)
    assert raw == intent_path.read_bytes()
    assert intent.suite == "demo"
    assert intent.case == "thing"


def test_malformed_json_is_refused(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(DeadeyeError, match="not valid JSON"):
        load_intent_file(path)


def test_a_utf8_bom_is_tolerated_and_the_raw_bytes_keep_it(tmp_path) -> None:
    # Editors on some platforms still save with a leading BOM; the document
    # must parse, while evidence keeps hashing the file's exact bytes.
    document = '{"purpose": "tourner la pièce", "subject": "café"}'
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + document.encode("utf-8"))
    intent, raw = load_intent_file(path)
    assert raw == path.read_bytes()
    assert intent.purpose == "tourner la pièce"
    assert intent.subject == "café"


def test_inline_intent_text_with_a_leading_bom_parses() -> None:
    intent, _ = parse_intent_text("\ufeff" + json.dumps({"purpose": "x"}))
    assert intent.purpose == "x"


def test_deeply_nested_json_is_refused_not_crashed() -> None:
    # Nesting beyond the interpreter limit must read as a malformed document,
    # not escape as RecursionError.
    with pytest.raises(DeadeyeError):
        parse_intent_text('{"purpose": ' + "[" * 20000 + "]" * 20000 + "}")


def test_redact_drops_credential_keys_nested() -> None:
    value = {"ok": 1, "api_key": "secret", "headers": {"Authorization": "Bearer x", "meta": "y"}}
    assert redact(value) == {"ok": 1, "headers": {"meta": "y"}}


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
