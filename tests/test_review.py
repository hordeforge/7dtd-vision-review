"""`run_review` orchestration: order, consent, limits, evidence."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deadeye.errors import DeadeyeError
from deadeye.providers.base import ProviderLimits, ReviewResponse
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


def test_review_envelope_records_utc_instant_and_monotonic_elapsed(clip_dir, intent_path) -> None:
    """The live path stamps UTC and records a non-negative elapsed duration."""
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
    )
    parsed = datetime.fromisoformat(envelope["created_utc"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    elapsed = envelope["provider"]["elapsed_seconds"]
    assert isinstance(elapsed, float)
    assert elapsed >= 0


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


def test_an_output_directory_refuses_before_any_submission(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """A directory cannot become an evidence file, even with --force."""
    output = tmp_path / "evidence"
    output.mkdir()

    submissions: list[object] = []
    provider = FakeProvider()
    real_review = provider.review

    def counting(request):
        submissions.append(request)
        return real_review(request)

    monkeypatch.setattr(provider, "review", counting)
    with pytest.raises(DeadeyeError, match="not a regular file"):
        run_review(
            clip_dir,
            provider=provider,
            intent_path=intent_path,
            allow_network=True,
            output=output,
            force=True,
        )
    assert submissions == []


def test_a_failed_evidence_write_still_delivers_the_billed_verdict(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """A write fault after a completed submission must not discard the
    verdict: the refusal carries the full envelope, so recovering it never
    means resubmitting (and re-billing) the same media."""
    from deadeye import evidence
    from deadeye.errors import EvidenceWriteError

    output = tmp_path / "evidence.json"

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evidence, "_atomic_write", no_space)
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
    # The key a successful run carries, on the path that hands back a billed
    # verdict to be recovered: a consumer reading it must not have to know
    # which of two envelope shapes it got.
    assert document["evidence"] == {"path": str(output), "sha256": None}


def test_every_envelope_carries_the_evidence_key(clip_dir, intent_path, tmp_path) -> None:
    """The persisted document and the returned envelope are the same shape:
    `evidence` is present in both, so a consumer parsing either one reads the
    same keys. The digest stays out of the file, which cannot contain its own
    hash, and rides the returned envelope."""
    output = tmp_path / "evidence.json"
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
    )
    document = json.loads(output.read_text())
    assert set(document) == set(envelope)
    assert document["evidence"]["path"] == str(output)
    assert document["evidence"]["sha256"] is None
    assert envelope["evidence"]["path"] == str(output)
    assert envelope["evidence"]["sha256"]


def test_evidence_is_written_and_hashes_address_it(clip_dir, intent_path, tmp_path) -> None:
    import hashlib
    from pathlib import Path

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
        # The digest has to address the bytes that were actually submitted:
        # a length check passes for any 64 hex characters, so a stale or
        # placeholder hash would go unnoticed.
        path = Path(entry["path"])
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["bytes"] == path.stat().st_size
    assert document["intent"]["sha256"] == hashlib.sha256(intent_path.read_bytes()).hexdigest()
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
    from deadeye import evidence

    output = tmp_path / "evidence.json"

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evidence, "_atomic_write", no_space)
    with pytest.raises(
        DeadeyeError, match=r"cannot write evidence file .*No space left"
    ) as exc_info:
        evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)
    # The OS fault is preserved as the cause, never swallowed by the wrap.
    assert isinstance(exc_info.value.__cause__, OSError)
    assert list(tmp_path.glob("*.tmp")) == []


def test_an_interrupted_evidence_replace_does_not_strand_a_temp_file(tmp_path, monkeypatch) -> None:
    """A non-OSError during the final rename used to skip the unlink path,
    leaving a `.tmp` sibling beside the destination. Every exit except a
    successful replace must delete the temporary file."""
    from pathlib import Path

    from deadeye.evidence import write_evidence

    def boom(self: Path, target: Path) -> Path:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        write_evidence(tmp_path / "evidence.json", {"kind": "deadeye-review"}, force=False)
    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "evidence.json").exists()


def test_exclusive_reserve_refuses_a_name_another_writer_already_holds(tmp_path) -> None:
    """O_CREAT|O_EXCL is the no-overwrite lock: a second reserve of the same
    path must fail even when the occupant is only the empty placeholder the
    first writer has not yet replaced."""
    from deadeye.evidence import _reserve_exclusive

    output = tmp_path / "evidence.json"
    _reserve_exclusive(output)
    assert output.is_file()
    assert output.stat().st_size == 0
    with pytest.raises(DeadeyeError, match="write in progress"):
        _reserve_exclusive(output)


def test_a_crash_stranded_placeholder_is_reclaimed_and_the_path_writes(
    clip_dir, intent_path, tmp_path, monkeypatch
) -> None:
    """A run killed between the exclusive reserve and the replace leaves an
    empty placeholder. It holds no review, so the next run must converge and
    write there instead of refusing a path that only ever held a reservation.

    The reserve is what creates the stranded state, so the test creates it
    the same way rather than fabricating a file."""
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    evidence._reserve_exclusive(output)
    assert output.stat().st_size == 0
    stale = time.time() - evidence._STALE_PLACEHOLDER_SECONDS - 1
    os.utime(output, (stale, stale))

    # The preflight no longer blocks the recovery run, and the review lands.
    evidence.ensure_writable(output, force=False)
    envelope = run_review(
        clip_dir,
        provider=FakeProvider(),
        intent_path=intent_path,
        allow_network=True,
        output=output,
    )
    assert json.loads(output.read_text(encoding="utf-8"))["review_id"] == envelope["review_id"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_live_placeholder_is_never_reclaimed_by_a_concurrent_writer(tmp_path) -> None:
    """Reclaiming is age-based, so a writer that reserved moments ago keeps
    its name: the concurrent-duplicate guarantee is untouched by the crash
    recovery, and the refusal names the real reason (a write in progress,
    not an earlier review)."""
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    evidence._reserve_exclusive(output)
    with pytest.raises(DeadeyeError, match="write in progress"):
        evidence.ensure_writable(output, force=False)
    with pytest.raises(DeadeyeError, match="write in progress"):
        evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)


def test_a_forward_wall_clock_step_never_frees_a_live_placeholder(tmp_path, monkeypatch) -> None:
    """A wall clock that jumps forward must not turn a reservation into a
    reclaimable one.

    An NTP correction, a manual `date -s`, or a VM restored from a snapshot
    can move the clock hours ahead between one writer's reserve and the next
    writer's preflight. The wall clock is the only clock that can be compared
    with an on-disk mtime, so the reclaim reads it, but the age it computes is
    held against this process's own monotonic time: a step can only make a
    placeholder read fresher and the run refuse, never free the name a live
    writer still holds."""
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    evidence._reserve_exclusive(output)
    real_now = time.time()

    stepped = real_now + 7200
    assert stepped - output.stat().st_mtime > evidence._STALE_PLACEHOLDER_SECONDS
    monkeypatch.setattr(evidence.time, "time", lambda: stepped)

    with pytest.raises(DeadeyeError, match="write in progress"):
        evidence.ensure_writable(output, force=False)
    with pytest.raises(DeadeyeError, match="write in progress"):
        evidence._reserve_exclusive(output)

    # Crash recovery still works through the same step: a placeholder that was
    # already stranded stays reclaimable, because its mtime predates the step
    # by more than the threshold either way.
    stranded = tmp_path / "stranded.json"
    evidence._reserve_exclusive(stranded)
    old = real_now - evidence._STALE_PLACEHOLDER_SECONDS - 1
    os.utime(stranded, (old, old))
    assert evidence._reserve_exclusive(stranded) is not None


def test_a_published_envelope_is_never_reclaimed_however_old_it_is(tmp_path) -> None:
    """Only an empty placeholder is reclaimable. Real evidence, however old,
    still ends a rerun without --force: the age rule must not become a way to
    overwrite an earlier review by waiting."""
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)
    old = time.time() - evidence._STALE_PLACEHOLDER_SECONDS - 3600
    os.utime(output, (old, old))

    with pytest.raises(DeadeyeError, match="already holds an earlier review"):
        evidence.ensure_writable(output, force=False)
    with pytest.raises(DeadeyeError, match="already holds an earlier review"):
        evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "deadeye-review"


def _payload(document) -> bytes:
    """The bytes `write_evidence` hands `_atomic_write`.

    The publish hashes and writes the same buffer, so a direct call has to
    encode too; passing text here is the type error the writer would refuse.
    """
    return json.dumps(document, indent=2, sort_keys=True).encode("utf-8")


def test_reclaiming_a_stale_placeholder_never_unlinks_a_review_published_after_the_check(
    tmp_path, monkeypatch
) -> None:
    """Age is not proof of death, so the reclaim must act on the file it saw.

    A writer stalled past `_STALE_PLACEHOLDER_SECONDS` between its reserve and
    its replace can publish in the window between the reclaiming writer's stat
    and its unlink. Unlinking by name there would delete a review that was
    never overwritten on purpose, and the reclaiming writer's own replace
    would then hide the deletion. The reclaim is fenced by identity, so the
    late publisher keeps its envelope and this run refuses instead.
    """
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    evidence._reserve_exclusive(output)
    old = time.time() - evidence._STALE_PLACEHOLDER_SECONDS - 1
    os.utime(output, (old, old))

    real_stat = evidence._stale_placeholder_stat

    def stat_then_publish(path):
        inspected = real_stat(path)
        assert inspected is not None, "the placeholder must be reclaimable for this race"
        # The stalled writer resumes and publishes between the check and the
        # unlink that would follow it.
        evidence.write_evidence(path, {"kind": "theirs"}, force=True)
        return inspected

    monkeypatch.setattr(evidence, "_stale_placeholder_stat", stat_then_publish)
    # `_atomic_write` directly: the reclaiming writer is past the preflight,
    # so the patched stat is the one the reclaim itself makes.
    with pytest.raises(DeadeyeError, match="already holds an earlier review"):
        evidence._atomic_write(output, _payload({"kind": "deadeye-review"}), force=False)

    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "theirs"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_write_never_unlinks_another_writers_review_from_its_placeholder(
    tmp_path, monkeypatch
) -> None:
    """The placeholder cleanup is fenced by the file this call created.

    A writer that holds a placeholder clears it on every failed path, and the
    name it clears is shared: a `--force` writer, or the reclaiming writer that
    took the name over, can have published a review into it first. Unlinking by
    name there deletes a review nobody chose to overwrite, and this write's own
    outcome cannot report it. The identity reserved here says which file is
    still ours to clear.

    The publication lands at the instant of this write's own replace, which is
    after the fence that refuses an earlier one read the placeholder it still
    held, so this exercises the cleanup and not that earlier refusal.
    """
    from pathlib import Path

    from deadeye import evidence

    output = tmp_path / "evidence.json"
    real_replace = Path.replace

    def publish_then_fail(self, target):
        # Another process replaces the placeholder this call is holding, at
        # the moment this call publishes, and this write fails there.
        theirs = tmp_path / "theirs.tmp"
        theirs.write_bytes(_payload({"kind": "theirs"}))
        real_replace(theirs, output)
        assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "theirs"
        raise RuntimeError("interrupted")

    monkeypatch.setattr(Path, "replace", publish_then_fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)

    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "theirs"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_force_run_publishing_into_a_reserved_name_is_never_overwritten(
    tmp_path, monkeypatch
) -> None:
    """A default run refuses rather than clobbering what a force run published.

    `--force` takes no reserve, so a force run's replace can land on the
    placeholder a default run is holding, between that run's reserve and its
    own publish. An unfenced publish would then overwrite an envelope nobody
    chose to replace and leave the force run holding a digest for bytes the
    file no longer has. The publish is fenced by the same identity the
    reclaim and the cleanup use, so the default run refuses and the force
    run's envelope stays, with no temp file stranded beside it.
    """
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    real_reserve = evidence._reserve_exclusive

    def reserve_then_publish(path):
        identity = real_reserve(path)
        # A --force run replaces our placeholder in the window between the
        # reserve and the publish.
        evidence._atomic_write(path, _payload({"kind": "theirs"}), force=True)
        return identity

    monkeypatch.setattr(evidence, "_reserve_exclusive", reserve_then_publish)
    with pytest.raises(DeadeyeError, match="published into by another writer"):
        evidence.write_evidence(output, {"kind": "ours"}, force=False)

    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "theirs"
    assert list(tmp_path.glob("*.tmp")) == []


def _race_two_writers(tmp_path, monkeypatch, gate) -> list[str]:
    """Write two envelopes to one path from two threads; return their outcomes."""
    from deadeye import evidence

    output = tmp_path / "evidence.json"
    original = evidence._atomic_write
    monkeypatch.setattr(evidence, "_atomic_write", gate(original))
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(tag: str) -> None:
        try:
            evidence.write_evidence(output, {"kind": tag}, force=False)
            result = "ok"
        except DeadeyeError as exc:
            result = str(exc)
        with lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=worker, args=("first",)),
        threading.Thread(target=worker, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_two_writers_that_both_pass_the_preflight_keep_exactly_one_envelope(
    tmp_path, monkeypatch
) -> None:
    """The second writer must not replace the first envelope, and must say why.

    Both threads clear `ensure_writable` before either publishes, so the
    write-time `O_CREAT|O_EXCL` reserve is the only thing standing between the
    two. The gate holds the second writer at the write until the first has
    published, which is what makes the outcome deterministic: a barrier alone
    only lines the two up, and the loser then raced the winner's replace, so
    the refusal it read was a coin flip between "an earlier review" and "a
    write in progress"."""
    published = threading.Event()

    def gate(original):
        def gated(path, payload, *, force):
            document = json.loads(payload)
            if document["kind"] == "second":
                assert published.wait(timeout=5), "the first writer never published"
            result = original(path, payload, force=force)
            if document["kind"] == "first":
                published.set()
            return result

        return gated

    output = tmp_path / "evidence.json"
    outcomes = _race_two_writers(tmp_path, monkeypatch, gate)

    ok = [item for item in outcomes if item == "ok"]
    refused = [item for item in outcomes if item != "ok"]
    assert len(ok) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert "already holds an earlier review" in refused[0]
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "first"
    assert list(tmp_path.glob("*.tmp")) == []


def test_two_writers_reaching_the_reserve_together_keep_exactly_one_envelope(
    tmp_path, monkeypatch
) -> None:
    """The same race with neither writer given a head start.

    Whichever writer wins the reserve publishes; the other is refused for the
    one reason that holds at the instant it looks, an empty placeholder still
    in flight or the envelope that beat it. Either message is correct here,
    and the invariant is the same: one envelope on disk, no stranded temp
    file, and the loser never overwrote the winner."""
    barrier = threading.Barrier(2, timeout=5)

    def gate(original):
        def gated(path, payload, *, force):
            barrier.wait()
            return original(path, payload, force=force)

        return gated

    output = tmp_path / "evidence.json"
    outcomes = _race_two_writers(tmp_path, monkeypatch, gate)

    ok = [item for item in outcomes if item == "ok"]
    refused = [item for item in outcomes if item != "ok"]
    assert len(ok) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert any(
        reason in refused[0] for reason in ("already holds an earlier review", "write in progress")
    ), refused[0]
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] in {"first", "second"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_evidence_write_does_not_follow_a_precreated_temp_symlink(tmp_path) -> None:
    """A stale predictable temp name must not redirect an evidence write."""
    from deadeye.evidence import write_evidence

    output = tmp_path / "evidence.json"
    protected = tmp_path / "protected.txt"
    protected.write_text("do not overwrite", encoding="utf-8")
    try:
        output.with_name(output.name + ".tmp").symlink_to(protected)
    except OSError:
        pytest.skip("this host cannot create the attacker symlink")

    write_evidence(output, {"kind": "deadeye-review"}, force=False)

    assert protected.read_text(encoding="utf-8") == "do not overwrite"
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "deadeye-review"


def test_the_publish_syncs_the_directory_after_the_rename(tmp_path, monkeypatch) -> None:
    """`fsync` on the payload fixes the bytes, not the name.

    A crash between the replace and the journal committing the directory
    entry can leave the destination holding what it held before, which here
    is the empty reserve. The caller has been handed the envelope and its
    digest by then, and the next run finds a zero-byte placeholder it must
    age out before it can publish at all.
    """
    from deadeye import evidence

    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("this platform has no directory handle to sync")

    order: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def spy_fsync(fd):
        order.append("fsync")
        real_fsync(fd)

    def spy_replace(self, target):
        order.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(evidence.os, "fsync", spy_fsync)
    monkeypatch.setattr(Path, "replace", spy_replace)

    evidence.write_evidence(tmp_path / "evidence.json", {"kind": "deadeye-review"}, force=False)

    assert order.index("replace") < len(order) - 1 - order[::-1].index("fsync"), order
    # The payload itself is synced too, and it is synced first: the bytes
    # must be on the medium before the name that claims they are there.
    assert order.count("fsync") >= 2, order
    assert order[0] == "fsync", order


def test_a_directory_sync_failure_is_reported_rather_than_swallowed(tmp_path, monkeypatch) -> None:
    """A publish that cannot be made durable must not read as a clean one.

    The envelope is on disk when the sync fails, so the refusal is what keeps
    a caller from treating a name a power cut may take back as a stored
    review.
    """
    from deadeye import evidence

    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("this platform has no directory handle to sync")

    output = tmp_path / "evidence.json"
    real_fsync = os.fsync

    def fail_on_directory(fd):
        if os.fstat(fd).st_mode & stat.S_IFDIR:
            raise OSError(errno.EIO, "directory sync failed")
        real_fsync(fd)

    monkeypatch.setattr(evidence.os, "fsync", fail_on_directory)

    with pytest.raises(DeadeyeError, match="cannot write evidence file"):
        evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)

    # The refusal is about durability, not about the bytes: the envelope is
    # published and a later run is refused as an occupied name, which is the
    # truth about what is on disk.
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "deadeye-review"
    assert list(tmp_path.glob("*.tmp")) == []


def test_the_directory_sync_does_not_run_on_a_platform_without_one(tmp_path, monkeypatch) -> None:
    """`O_DIRECTORY` is not everywhere. A platform with no directory handle has
    nothing to sync, and the publish must not fail looking for one."""
    from deadeye import evidence

    monkeypatch.delattr(evidence.os, "O_DIRECTORY", raising=False)

    output = tmp_path / "evidence.json"
    evidence.write_evidence(output, {"kind": "deadeye-review"}, force=False)

    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == "deadeye-review"


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


def test_every_fault_after_the_submission_says_the_key_is_spent(
    clip_dir, intent_path, monkeypatch
) -> None:
    """Once the media has been sent, no outcome is free to repeat.

    A transport that deduplicates (the MCP idempotency ledger) can only record
    a call as spent if it can tell a submitted call from one the provider
    never saw, and the only way it can tell is the exception type. A refusal
    raised before the submission stays an ordinary `DeadeyeError` and leaves
    the key free; a timeout and an answer the result schema rejects are both
    billed attempts, so both are `NoVerdictError`.
    """
    from deadeye.errors import NoVerdictError

    provider = FakeProvider()

    def slow_review(request):
        raise TimeoutError("timed out")

    monkeypatch.setattr(provider, "review", slow_review)
    with pytest.raises(NoVerdictError, match="new billable review, not a retry"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)

    def unusable_review(request):
        return ReviewResponse(raw_text="not json at all", usage=None, model_reported="fake")

    monkeypatch.setattr(provider, "review", unusable_review)
    with pytest.raises(NoVerdictError, match="structural validation"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)

    # Nothing was submitted above, so the same key is still unused.
    with pytest.raises(DeadeyeError) as local:
        run_review(
            clip_dir,
            provider=FakeProvider(),
            intent_path=clip_dir / "missing.json",
            allow_network=True,
        )
    assert not isinstance(local.value, NoVerdictError)


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


class _WireBudgetFake(FakeProvider):
    """The fake provider wearing a request budget that fits the raw byte
    total but not the base64-encoded one."""

    @property
    def limits(self) -> ProviderLimits:
        declared = self._limits
        return ProviderLimits(
            suffixes=declared.suffixes,
            max_bytes=40,
            max_frames=declared.max_frames,
            accepts_video=declared.accepts_video,
            max_video_bytes=declared.max_video_bytes,
        )


def test_the_request_budget_counts_base64_wire_size_not_raw_bytes(clip_dir, intent_path) -> None:
    """Eight submitted frames hold 32 raw bytes but 64 once inline base64
    encodes them; a 40-byte per-request budget must refuse locally, where a
    raw-byte comparison would wave the submission through to a provider-side
    refusal after the upload."""
    provider = _WireBudgetFake()
    with pytest.raises(DeadeyeError, match=r"32 bytes \(64 as submitted base64\)"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)
    assert provider.requests == []


class _PromptBudgetFake(FakeProvider):
    """A per-request budget that fits the encoded media and nothing beside it."""

    @property
    def limits(self) -> ProviderLimits:
        declared = self._limits
        return ProviderLimits(
            suffixes=declared.suffixes,
            max_bytes=512,
            max_frames=declared.max_frames,
            accepts_video=declared.accepts_video,
            max_video_bytes=declared.max_video_bytes,
        )


def test_the_request_budget_counts_the_prompt_that_rides_the_request(clip_dir, intent_path) -> None:
    """The prompt is part of the same JSON body as the media: a budget that
    covers the 64 encoded media bytes but not the prompt beside them must
    refuse here, not after an upload the provider would reject with 400."""
    provider = _PromptBudgetFake()
    with pytest.raises(DeadeyeError, match=r"prompt included"):
        run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)
    assert provider.requests == []


def test_an_over_budget_request_is_refused_before_any_attachment_is_read(
    clip_dir, intent_path, monkeypatch
) -> None:
    """The size preflight protects a large invalid request from needless disk
    reads and from retaining every attachment in memory before refusing it."""
    from deadeye import review

    def should_not_read(path):
        raise AssertionError(f"over-budget request read {path}")

    monkeypatch.setattr(review, "sha256_file", should_not_read)
    with pytest.raises(DeadeyeError, match=r"32 bytes \(64 as submitted base64\)"):
        run_review(
            clip_dir,
            provider=_WireBudgetFake(),
            intent_path=intent_path,
            allow_network=True,
        )


class _WholeRequestBudgetFake(FakeProvider):
    """A request budget that fits the muxed video's own media and nothing else.

    `max_video_bytes` stays the fake's generous default, so the sampling layer
    picks the video; `max_bytes` is where the test puts it, which is the only
    shape in which the whole-request budget is what refuses.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        super().__init__()

    @property
    def limits(self) -> ProviderLimits:
        declared = self._limits
        return ProviderLimits(
            suffixes=declared.suffixes,
            max_bytes=self._max_bytes,
            max_frames=declared.max_frames,
            accepts_video=declared.accepts_video,
            max_video_bytes=declared.max_video_bytes,
        )


_VIDEO_RAW_BYTES = 256 * 1024
_VIDEO_WIRE_BYTES = 4 * ((_VIDEO_RAW_BYTES + 2) // 3)
_FRAMES_WIRE_BYTES = 64  # the fake's 8 sampled frames of 4 raw bytes each


def _prompt_wire_bytes(clip_dir, intent_path) -> int:
    """What the prompt costs on the wire, measured from a real submission.

    Measured here with `json.dumps` rather than through
    `deadeye.review._json_string_bytes`: the budget this feeds is checked by
    the function under test, so importing it would let an undercounting
    implementation pick a threshold that fits its own mistake.
    """
    provider = FakeProvider()
    run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)
    request = provider.requests[-1]
    return sum(
        len(json.dumps(text).encode("utf-8")) for text in (request.system_prompt, request.prompt)
    )


def test_a_video_that_only_the_prompt_pushes_over_falls_back_to_the_frames(
    clip_dir, intent_path
) -> None:
    """The muxed video clears the provider's own video budget and the frames
    beside it are a fraction of its size, so the whole-request cap is the only
    thing that refuses, and only by the prompt riding beside the media. A
    gateway that picks the video and then refuses outright throws away a
    review the frames in the same directory would have carried; the evidence
    has to say which bytes were actually sent."""
    (clip_dir / "clip.mp4").write_bytes(b"\x00" * _VIDEO_RAW_BYTES)
    # One byte below what the video plus the prompt costs, and exactly what
    # the sampled frames plus that same prompt cost.
    provider = _WholeRequestBudgetFake(
        _VIDEO_WIRE_BYTES + _prompt_wire_bytes(clip_dir, intent_path) - _FRAMES_WIRE_BYTES
    )
    envelope = run_review(clip_dir, provider=provider, intent_path=intent_path, allow_network=True)

    submitted = {payload.kind for payload in provider.requests[-1].media}
    assert submitted == {"frame"}, "the video must not ride the request that was sent"
    assert envelope["sampling"]["frames_submitted"] == 8
    assert "sampled frames instead" in envelope["sampling"]["note"]
    assert envelope["media"][0]["path"].endswith(".png")
    assert envelope["disclosure"]["total_bytes"] == 8 * 4


def test_a_video_only_the_prompt_pushes_over_still_refuses_without_frames(
    clip_dir_with_video, intent_path
) -> None:
    """The fallback needs frames to fall back to. With none beside the video
    the honest answer is the refusal, and it is raised before the submission
    rather than after a billed upload the provider would reject."""
    for index in range(10):
        (clip_dir_with_video / f"frame-{index:04d}.png").unlink()
    provider = _WholeRequestBudgetFake(
        20 + _prompt_wire_bytes(clip_dir_with_video, intent_path) - 1
    )
    with pytest.raises(DeadeyeError, match="prompt included"):
        run_review(
            clip_dir_with_video, provider=provider, intent_path=intent_path, allow_network=True
        )
    assert provider.requests == []
