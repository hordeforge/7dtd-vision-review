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


def test_a_rerun_into_an_occupied_output_refuses_before_any_submission(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """The overwrite guard is checked before anything is contacted: a plain
    rerun into an existing --output is refused for free instead of paying
    for a full billable submission and only then refusing to write."""
    output = tmp_path / "evidence.json"
    output.write_text("{}")

    submissions: list[object] = []
    provider = FakeProvider()
    real_review = provider.review

    def counting(request):
        submissions.append(request)
        return real_review(request)

    monkeypatch.setattr(provider, "review", counting)
    with pytest.raises(DeadeyeError, match="already holds an earlier review"):
        run_review(
            clip_dir,
            provider=provider,
            intent_path=intent_path,
            allow_network=True,
            output=output,
        )
    assert submissions == []


def test_a_failed_evidence_write_still_delivers_the_billed_verdict(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """A write fault after a completed submission must not discard the
    verdict: the refusal carries the full envelope, so recovering it never
    means resubmitting (and re-billing) the same media."""
    from pathlib import Path

    from deadeye.errors import EvidenceWriteError

    output = tmp_path / "evidence.json"

    def no_space(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(EvidenceWriteError) as exc_info:
        run_review(
            clip_dir,
            provider=FakeProvider(),
            intent_path=intent_path,
            allow_network=True,
            output=output,
        )
    document = exc_info.value.document
    assert document["kind"] == "deadeye-review"
    assert document["result"]["summary"]
    assert document["provider"]["name"] == "fake"


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


def test_evidence_bytes_are_the_hashed_utf8_on_every_platform(
    clip_dir, intent_path, tmp_path
) -> None:
    """The stored envelope is exactly the UTF-8 bytes its sha256 hashes: a
    text-mode write would let platform newline translation (CRLF) rewrite
    them on disk and silently break hash addressing."""
    import hashlib

    output = tmp_path / "evidence.json"
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
    )
    raw = output.read_bytes()
    assert b"\r" not in raw
    assert hashlib.sha256(raw).hexdigest() == envelope["evidence"]["sha256"]
    json.loads(raw.decode("utf-8"))


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


def test_a_json_raw_response_is_actually_redacted_before_it_is_kept(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """`--keep-raw-response` claims a *redacted* copy; a raw response is a
    string, so the mapping-walking backstop must be applied to its parsed
    contents or credential-named keys ride straight into stored evidence."""
    provider = FakeProvider()
    original = provider.review

    def echoing_review(request):
        from deadeye.providers.base import ReviewResponse

        original(request)
        return ReviewResponse(
            raw_text='{"summary": "verdict", "api_key": "nvapi-echoed"}',
            usage=None,
            model_reported=None,
        )

    monkeypatch.setattr(provider, "review", echoing_review)
    output = tmp_path / "evidence.json"
    with pytest.raises(DeadeyeError, match="redacted raw"):
        run_review(
            clip_dir,
            provider=provider,
            intent_path=intent_path,
            allow_network=True,
            keep_raw_response=True,
            output=output,
        )
    document = json.loads(output.read_text())
    assert "nvapi-echoed" not in document["raw_provider_response"]
    assert '"summary": "verdict"' in document["raw_provider_response"]


def test_non_json_prose_in_a_kept_raw_response_stays_byte_identical(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """Redaction may only rewrite structure-shaped text: model prose that
    fails to parse must survive exactly as the provider sent it."""
    prose = "I could not produce JSON today; here is my verdict in words."
    provider = FakeProvider()
    original = provider.review

    def prosing_review(request):
        from deadeye.providers.base import ReviewResponse

        original(request)
        return ReviewResponse(raw_text=prose, usage=None, model_reported=None)

    monkeypatch.setattr(provider, "review", prosing_review)
    output = tmp_path / "evidence.json"
    with pytest.raises(DeadeyeError, match="redacted raw"):
        run_review(
            clip_dir,
            provider=provider,
            intent_path=intent_path,
            allow_network=True,
            keep_raw_response=True,
            output=output,
        )
    document = json.loads(output.read_text())
    assert document["raw_provider_response"] == prose


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


def test_a_failed_evidence_write_strands_no_partial_temp_file(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """A write that dies midway (disk full, permissions) must not leave a
    corrupt `.tmp` beside the evidence directory; the original fault surfaces,
    wrapped in the one-refusal contract with the path named."""
    from pathlib import Path

    from deadeye.evidence import write_evidence

    output = tmp_path / "evidence.json"

    def no_space(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(
        DeadeyeError, match=r"cannot write evidence file .*No space left"
    ) as exc_info:
        write_evidence(output, {"kind": "deadeye-review"}, force=False)
    # The OS fault is preserved as the cause, never swallowed by the wrap.
    assert isinstance(exc_info.value.__cause__, OSError)
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_timeout_refusal_warns_that_resubmitting_bills_again(
    clip_dir, intent_path, monkeypatch
) -> None:
    """A timeout is ambiguous: the provider may have completed and billed the
    attempt server-side. The refusal must say that resubmitting starts a new
    billable review, so no caller mistakes it for a safe retry."""
    provider = FakeProvider()

    def slow_review(request):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider, "review", slow_review)
    with pytest.raises(DeadeyeError, match="new billable review, not a retry"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)


def test_rerunning_a_review_preserves_both_envelopes_as_independent_evidence(
    clip_dir, intent_path, tmp_path
) -> None:
    """Two executions of the same review never converge into one artifact:
    each run submits again and writes its own envelope under its own
    `review_id`, and the first file is untouched by the second run."""
    import json

    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"
    first = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=first_output,
    )
    second = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=second_output,
    )
    assert first["review_id"] != second["review_id"]
    assert json.loads(first_output.read_text())["review_id"] == first["review_id"]
    assert json.loads(second_output.read_text())["review_id"] == second["review_id"]


def test_disclosure_counts_every_submitted_copy_of_a_file(clip_dir, tmp_path) -> None:
    """The same reference listed twice is uploaded twice: the disclosure and
    the evidence must count every byte that leaves the machine, not unique
    paths."""
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"A" * 500)
    intent = tmp_path / "i.json"
    intent.write_text(
        json.dumps(
            {
                "purpose": "p",
                "references": [
                    {"path": str(reference), "purpose": "a"},
                    {"path": str(reference), "purpose": "b"},
                ],
            }
        ),
        encoding="utf-8",
    )
    provider = FakeProvider()
    envelope = run_review(clip_dir, provider=provider, intent_path=intent, allow_network=True)
    request = provider.requests[-1]
    actual_bytes = sum(len(payload.data) for payload in request.media)
    assert envelope["disclosure"]["total_bytes"] == actual_bytes
    assert len(envelope["media"]) == len(request.media)
