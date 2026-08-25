"""`run_review` orchestration: order, consent, limits, evidence."""

from __future__ import annotations

import json

import pytest

from deadeye.errors import DeadeyeError
from deadeye.providers.fake import FakeProvider
from deadeye.review import run_review


def test_consent_is_demanded_before_credentials_are_even_read(
    clip_dir, intent_path, monkeypatch
) -> None:
    provider = FakeProvider()

    def unreachable() -> bool:
        raise AssertionError("is_configured must not be reached before consent")

    monkeypatch.setattr(provider, "is_configured", unreachable)
    with pytest.raises(DeadeyeError, match="--allow-network"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=False)


def test_exactly_one_intent_source_is_required(clip_dir, intent_path) -> None:
    provider = FakeProvider()
    with pytest.raises(DeadeyeError, match="exactly one of --intent"):
        run_review(
            clip_dir,
            provider=provider,
            intent_path=intent_path,
            intent_text="{}",
            allow_network=True,
        )
    with pytest.raises(DeadeyeError, match="exactly one of --intent"):
        run_review(clip_dir, provider=provider, allow_network=True)


def test_clip_must_exist(intent_path, tmp_path) -> None:
    with pytest.raises(DeadeyeError, match="no such clip"):
        run_review(
            tmp_path / "missing",
            provider=FakeProvider(),
            intent_path=intent_path,
            allow_network=True,
        )


def test_an_earlier_evidence_envelope_is_never_overwritten_by_default(
    clip_dir, intent_path, tmp_path
) -> None:
    output = tmp_path / "evidence.json"
    run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
    )
    with pytest.raises(DeadeyeError, match="never overwrites"):
        run_review(
            clip_dir,
            provider=FakeProvider(),
            intent_path=intent_path,
            allow_network=True,
            output=output,
        )
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
        force=True,
    )
    assert envelope["evidence"]["path"] == str(output)


def test_evidence_is_written_and_hashes_address_it(clip_dir, intent_path, tmp_path) -> None:
    import hashlib

    output = tmp_path / "evidence.json"
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
    )
    document = json.loads(output.read_text())
    assert document["kind"] == "deadeye-review"
    assert document["media"], "every submitted file is hashed into evidence"
    for entry in document["media"]:
        assert len(entry["sha256"]) == 64
    assert document["intent"]["sha256"]
    assert document["provider"]["name"] == "fake"
    assert envelope["evidence"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_credentials_never_appear_in_evidence(clip_dir, intent_path, tmp_path) -> None:
    output = tmp_path / "evidence.json"
    run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
        keep_raw_response=True,
    )
    document = json.loads(output.read_text())
    assert "GEMINI" not in json.dumps(document)
    assert "api_key" not in json.dumps(document)


def test_invalid_structured_output_fails_validation(clip_dir, tmp_path, monkeypatch) -> None:

    provider = FakeProvider()
    original = provider.review

    def bad_review(request):
        from deadeye.providers.base import ReviewResponse

        original(request)
        return ReviewResponse(raw_text='{"summary": "broken"}', usage=None, model_reported=None)

    monkeypatch.setattr(provider, "review", bad_review)
    intent = tmp_path / "i.json"
    intent.write_text('{"purpose": "x"}')
    with pytest.raises(DeadeyeError, match="missing key"):
        run_review(clip_dir, provider=provider, intent_path=intent, allow_network=True)


def test_disclosure_is_announced_before_submission(clip_dir, intent_path, capsys) -> None:
    import sys

    def notify(line: str) -> None:
        print(line, file=sys.stderr)

    run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        notify=notify,
    )
    stderr = capsys.readouterr().err
    assert "provider: fake" in stderr
    assert "submitting 8 file(s)" in stderr
    assert "retention is governed" in stderr
